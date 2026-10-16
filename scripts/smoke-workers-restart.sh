# shellcheck shell=bash
# scripts/smoke-workers.sh から source される再起動シナリオ（単独では実行しない）。
# SMOKE_RESTART=1: worker kill / compose restart / temporal restart / postgres restart
# SMOKE_DOWN=1   : stop→up と down→up（**-v なし**）。前後で DB 件数・alembic・MinIO 件数・Schedule・volume を比べる
# 有料 queue には何も投げない。骨組み Episode（dummy-worker）だけを流す。

RESTART_BOUND="${RESTART_BOUND:-240}"

# 全 configured worker の poller が（新しい接続で）見えるまで待つ。経過秒を echo
wait_pollers() {
  local bound="$1" start=$SECONDS pending
  while :; do
    pending=()
    for s in "${CONFIGURED[@]}"; do poller_ok "$s" || pending+=("$s"); done
    [ ${#pending[@]} -eq 0 ] && { echo $((SECONDS - start)); return 0; }
    [ $((SECONDS - start)) -ge "$bound" ] && { echo "  not polling: ${pending[*]}" >&2; return 1; }
    sleep 3
  done
}

recover() {  # $1=label
  local label="$1" t
  if t="$(wait_pollers "$RESTART_BOUND")"; then ok "[$label] all configured workers polling again after ${t}s"
  else ng "[$label] workers did not resume polling within ${RESTART_BOUND}s"; fi
  check_configured_healthy "$label" "$RESTART_BOUND" || true
}

snapshot() {
  echo "db: $(psql_q "select 'episodes='||(select count(*) from episodes)||' artifact_metadata='||(select count(*) from artifact_metadata)||' jobs='||(select count(*) from jobs)")"
  echo "alembic: $(psql_q 'select version_num from alembic_version order by 1')"
  echo "minio_artifacts: $(docker exec avp2-minio-1 sh -c 'mc alias set s http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null && mc ls --recursive s/artifacts | wc -l')"
  echo "schedules: $(docker exec avp2-temporal-1 temporal schedule list --address temporal:7233 2>&1 | tr -s ' \n' ' ')"
  echo "volumes: $(docker volume ls -q | grep '^avp2_' | sort | tr '\n' ' ')"
}

num() { sed -n "s/.*$1=\([0-9]*\).*/\1/p"; }
compare_snapshot() {  # $1=before file $2=after file $3=label
  local b="$1" a="$2" label="$3"
  for k in episodes artifact_metadata jobs; do
    local x y; x="$(grep '^db:' "$b" | num "$k")"; y="$(grep '^db:' "$a" | num "$k")"
    if [ "$y" -ge "$x" ]; then ok "[$label] $k $x -> $y (not lost)"; else ng "[$label] $k $x -> $y (LOST)"; fi
  done
  local mb ma; mb="$(sed -n 's/^minio_artifacts: //p' "$b")"; ma="$(sed -n 's/^minio_artifacts: //p' "$a")"
  if [ "$ma" -ge "$mb" ]; then ok "[$label] MinIO artifacts objects $mb -> $ma"; else ng "[$label] MinIO objects $mb -> $ma (LOST)"; fi
  for k in alembic schedules volumes; do
    if [ "$(grep "^$k:" "$b")" = "$(grep "^$k:" "$a")" ]; then ok "[$label] $k unchanged ($(grep "^$k:" "$a" | cut -c1-80))"
    else ng "[$label] $k changed: $(grep "^$k:" "$b") => $(grep "^$k:" "$a")"; fi
  done
}

if [ "${SMOKE_RESTART:-}" = "1" ]; then
  # `docker kill` は Docker にとって手動停止扱いで unless-stopped でも再起動しない（実測）。
  # 障害（OOM kill・クラッシュ）と同じ形にするため、host から init（tini）を SIGKILL する
  echo "== restart a: SIGKILL the container init from host (pipeline-worker, render-worker) =="
  for s in pipeline-worker render-worker; do
    is_unconfigured "$s" && continue
    rc0="$(restart_count "$s")"; t0=$SECONDS
    kill -KILL "$(docker inspect -f '{{.State.Pid}}' "$(cid "$s")")"
    for _ in $(seq 1 60); do [ "$(restart_count "$s")" -gt "$rc0" ] && break; sleep 1; done
    rc1="$(restart_count "$s")"
    if [ "$rc1" -gt "$rc0" ]; then ok "[a] $s restarted by Docker after $((SECONDS - t0))s (RestartCount $rc0 -> $rc1)"
    else ng "[a] $s was not restarted by Docker (RestartCount $rc0)"; fi
    if wait_healthy "$RESTART_BOUND" "$s" >/dev/null; then ok "[a] $s healthy $((SECONDS - t0))s after kill"
    else ng "[a] $s not healthy within ${RESTART_BOUND}s"; fi
    poller_ok "$s" && ok "[a] $s polling" || ng "[a] $s not polling"
  done

  echo "== restart a2: worker python process SIGKILL from host (pipeline-worker) =="
  s=pipeline-worker; rc0="$(restart_count "$s")"; t0=$SECONDS
  wpid="$(docker top "$(cid "$s")" -eo pid,args | awk '/workers.pipeline.run_worker/ && !/tini|docker-init/ {print $1; exit}')"
  if [ -n "$wpid" ]; then
    kill -KILL "$wpid"
    for _ in $(seq 1 60); do [ "$(restart_count "$s")" -gt "$rc0" ] && break; sleep 1; done
    rc1="$(restart_count "$s")"
    [ "$rc1" -gt "$rc0" ] && ok "[a2] $s restarted after worker process kill in $((SECONDS - t0))s (RestartCount $rc0 -> $rc1)" \
      || ng "[a2] $s not restarted after process kill"
    wait_healthy "$RESTART_BOUND" "$s" >/dev/null && ok "[a2] $s healthy $((SECONDS - t0))s after kill" || ng "[a2] $s not healthy"
  else
    ng "[a2] worker python pid not found"
  fi

  echo "== restart b: docker compose restart pipeline-worker =="
  t0=$SECONDS; docker compose restart pipeline-worker >/dev/null
  wait_healthy "$RESTART_BOUND" pipeline-worker >/dev/null && poller_ok pipeline-worker \
    && ok "[b] pipeline-worker healthy+polling $((SECONDS - t0))s after restart" || ng "[b] pipeline-worker did not recover"

  echo "== restart c: docker compose restart temporal =="
  t0=$SECONDS; docker compose restart temporal >/dev/null
  wait_healthy "$RESTART_BOUND" temporal >/dev/null && ok "[c] temporal healthy after $((SECONDS - t0))s" || ng "[c] temporal not healthy"
  recover c
  ok "[c] total recovery $((SECONDS - t0))s"
  skeleton after-temporal-restart

  echo "== restart d: docker compose restart postgres =="
  t0=$SECONDS; docker compose restart postgres >/dev/null
  wait_healthy "$RESTART_BOUND" postgres >/dev/null && ok "[d] postgres healthy after $((SECONDS - t0))s" || ng "[d] postgres not healthy"
  recover d
  skeleton after-postgres-restart
fi

if [ "${SMOKE_DOWN:-}" = "1" ]; then
  snapshot >"$LOG_DIR/snap-before.txt"; cat "$LOG_DIR/snap-before.txt"

  echo "== restart e1: docker compose stop → docker compose up -d =="
  t0=$SECONDS; docker compose stop >/dev/null; ok "[e1] stop took $((SECONDS - t0))s"
  t0=$SECONDS; docker compose up -d >/dev/null
  check_configured_healthy e1 "$RESTART_BOUND" || true
  ok "[e1] up -d → healthy in $((SECONDS - t0))s"
  skeleton after-stop-up
  snapshot >"$LOG_DIR/snap-e1.txt"; compare_snapshot "$LOG_DIR/snap-before.txt" "$LOG_DIR/snap-e1.txt" e1

  echo "== restart e2: docker compose down (NO -v) → docker compose up -d =="
  t0=$SECONDS; docker compose down >/dev/null; ok "[e2] down took $((SECONDS - t0))s"
  t0=$SECONDS; docker compose up -d >/dev/null
  check_configured_healthy e2 "$RESTART_BOUND" || true
  ok "[e2] up -d → healthy in $((SECONDS - t0))s"
  skeleton after-down-up
  snapshot >"$LOG_DIR/snap-e2.txt"; compare_snapshot "$LOG_DIR/snap-before.txt" "$LOG_DIR/snap-e2.txt" e2
fi
