#!/usr/bin/env bash
# 常駐 Worker（compose, ADR-0024 / docs/operations/workers.md）の起動・健全性・停止スイッチを検証する。
# **有料・外部呼び出しは無い**。有料 queue（script / storyboard / production* / render* / upload*）には
# workflow を投げない。流すのは Phase 1 骨組み（dummy-worker）と、DB スイッチ paused を on にした
# DailyEpisodeWorkflow（子を起動せず paused で返る）と、一意な queue の使い捨て probe だけ。
#
# 使い方（worktree の .env に COMPOSE_PROFILES=core が要る。素の `docker compose up -d` で worker まで上がること）:
#   ./scripts/smoke-workers.sh                     # up -d → health → 未設定 worker の backoff → 骨組み → スイッチ
#   SMOKE_BUILD=1 ./scripts/smoke-workers.sh       # up -d --build
#   SMOKE_SKIP_UP=1 ./scripts/smoke-workers.sh     # 起動済みのスタックを検証するだけ
#   SMOKE_RESTART=1 ./scripts/smoke-workers.sh     # 続けて再起動シナリオ（worker kill / restart / temporal / postgres）
#   SMOKE_DOWN=1 ./scripts/smoke-workers.sh        # さらに stop→up と down→up（**-v は使わない**）でデータが残るか
#   EXPECTED_UNCONFIGURED="upload-worker" ...      # 設定不足で起動しない想定の worker（既定は .env/env から推定）
#
# volume は消さない。`ensure-daily-schedule.py --apply` は呼ばない。スイッチは終了時に元へ戻す。
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="${AVP_VENV:-../../.venv}"
[ -x "$VENV/bin/python" ] || { echo "NG: python venv not found" >&2; exit 1; }
VENV="$(cd "$VENV" && pwd)"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
UNCONF_OBSERVE_SECONDS="${UNCONF_OBSERVE_SECONDS:-150}"
SLOT_DATE="${SLOT_DATE:-2000-01-01}"
LOG_DIR="${LOG_DIR:-$(mktemp -d -t smoke-workers.XXXXXX)}"
mkdir -p "$LOG_DIR"

# service → 見張る queue（compose.yaml の healthcheck と同じ）
declare -A QUEUES=(
  [dummy-worker]="episode-skeleton"
  [script-worker]="script"
  [storyboard-worker]="storyboard"
  [production-worker]="production"
  [production-image-worker]="production-image"
  [production-voice-worker]="production-voice"
  [production-video-worker]="production-video"
  [render-worker]="render"   # compose healthcheck と同じ（media queue は長時間 activity 中に poll が途切れる）
  [upload-worker]="upload"
  [pipeline-worker]="pipeline"
)
ALL_WORKERS=(dummy-worker script-worker storyboard-worker production-worker production-image-worker
  production-voice-worker production-video-worker render-worker upload-worker pipeline-worker)

FAILS=0
ok() { echo "OK: $*"; }
ng() { echo "NG: $*" >&2; FAILS=$((FAILS + 1)); }
die() { echo "NG: $*" >&2; exit 1; }

# --- .env / env から値を読む（値は表示しない） ---
env_value() {
  local name="$1" v="${!1:-}"
  if [ -z "$v" ] && [ -f .env ]; then
    v="$(sed -n "s/^${name}=//p" .env | tail -n 1)"
  fi
  printf '%s' "$v"
}

if [ -z "${EXPECTED_UNCONFIGURED+x}" ]; then
  EXPECTED_UNCONFIGURED=""
  [ -n "$(env_value FAL_KEY)" ] || EXPECTED_UNCONFIGURED+=" production-image-worker production-video-worker"
  token_dir="$(env_value YOUTUBE_TOKEN_HOST_DIR)"; token_dir="${token_dir:-$HOME/.config/avp}"
  token_file="$(env_value YOUTUBE_REFRESH_TOKEN_FILE)"; token_file="${token_file:-youtube-refresh-token}"
  if [ -z "$(env_value YOUTUBE_CLIENT_ID)" ] || [ -z "$(env_value YOUTUBE_CLIENT_SECRET)" ] \
     || [ -z "$(env_value YOUTUBE_CHANNEL_ID)" ] || [ ! -f "$token_dir/$token_file" ]; then
    EXPECTED_UNCONFIGURED+=" upload-worker"
  fi
fi
is_unconfigured() { [[ " $EXPECTED_UNCONFIGURED " == *" $1 "* ]]; }
CONFIGURED=()
for w in "${ALL_WORKERS[@]}"; do is_unconfigured "$w" || CONFIGURED+=("$w"); done
echo "configured  : ${CONFIGURED[*]}"
echo "unconfigured: ${EXPECTED_UNCONFIGURED:-<none>}"
echo "logs        : $LOG_DIR"

[ "$(env_value COMPOSE_PROFILES)" = "core" ] \
  || die "COMPOSE_PROFILES=core is not set in .env/env (plain 'docker compose up -d' would not start workers)"

# --- host から DB / Temporal を触るための env（資格情報はコンテナから取り、表示しない） ---
host_env() {
  if [ -z "${DATABASE_URL:-}" ]; then
    local pg_pw
    pg_pw="$(docker exec avp2-postgres-1 printenv POSTGRES_PASSWORD)"
    export DATABASE_URL="postgresql+psycopg://avp:${pg_pw}@localhost:5432/avp"
  fi
  export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
  [ -n "${MINIO_ACCESS_KEY:-}" ] || MINIO_ACCESS_KEY="$(docker exec avp2-minio-1 printenv MINIO_ROOT_USER)"
  [ -n "${MINIO_SECRET_KEY:-}" ] || MINIO_SECRET_KEY="$(docker exec avp2-minio-1 printenv MINIO_ROOT_PASSWORD)"
  export MINIO_ACCESS_KEY MINIO_SECRET_KEY
  export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"
  export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
  export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
  # host の env の停止スイッチは DB スイッチの確認を曇らせるので明示的に false
  export PAUSED=false UPLOADS_PAUSED=false
}
py() { PYTHONPATH=. "$VENV/bin/python" "$@"; }
psql_q() { docker exec avp2-postgres-1 psql -U avp -d avp -Atc "$1"; }

cid() { docker compose ps -a -q "$1" 2>/dev/null | head -n 1; }
health() {
  local c; c="$(cid "$1")"
  [ -n "$c" ] || { echo "absent"; return; }
  docker inspect -f '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$c"
}
restart_count() { docker inspect -f '{{.RestartCount}}' "$(cid "$1")"; }
started_at() { docker inspect -f '{{.State.StartedAt}}' "$(cid "$1")"; }

# $1=bound seconds, 以降 service。全部 running/healthy になるまで待つ。経過秒を echo
wait_healthy() {
  local bound="$1"; shift
  local start=$SECONDS pending
  while :; do
    pending=()
    for s in "$@"; do [ "$(health "$s")" = "running/healthy" ] || pending+=("$s"); done
    [ ${#pending[@]} -eq 0 ] && { echo $((SECONDS - start)); return 0; }
    if [ $((SECONDS - start)) -ge "$bound" ]; then
      for s in "${pending[@]}"; do echo "  $s: $(health "$s")" >&2; done
      return 1
    fi
    sleep 3
  done
}

# 自分のコンテナが各 queue を poll しているか（コンテナ内の healthcheck と同じコマンド）
poller_ok() {
  local s="$1" args=()
  for q in ${QUEUES[$s]}; do args+=(--queue "$q"); done
  docker compose exec -T "$s" python -m infrastructure.temporal.poller_check "${args[@]}" >/dev/null 2>&1
}

check_configured_healthy() {
  local label="$1" bound="${2:-$WAIT_SECONDS}" took
  if took="$(wait_healthy "$bound" "${CONFIGURED[@]}")"; then
    ok "[$label] configured workers healthy in ${took}s (bound ${bound}s)"
  else
    ng "[$label] configured workers not healthy within ${bound}s"
    return 1
  fi
  for s in "${CONFIGURED[@]}"; do
    if poller_ok "$s"; then ok "[$label] $s polls ${QUEUES[$s]}"; else ng "[$label] $s poller_check failed"; fi
  done
}

# ---------------------------------------------------------------------------
# 1. 起動
# ---------------------------------------------------------------------------
if [ "${SMOKE_SKIP_UP:-}" != "1" ]; then
  up_args=(up -d)
  [ "${SMOKE_BUILD:-}" = "1" ] && up_args+=(--build)
  echo "== docker compose ${up_args[*]} =="
  docker compose "${up_args[@]}"
fi

echo "== every worker service exists after plain 'docker compose up -d' =="
for s in "${ALL_WORKERS[@]}"; do
  st="$(health "$s")"
  if [ "$st" = "absent" ]; then ng "$s container absent"; else ok "$s present ($st)"; fi
done
[ "$FAILS" -eq 0 ] || die "workers are not started by compose ($FAILS missing)"

echo "== configured workers healthy + polling =="
check_configured_healthy startup || die "startup health failed"
host_env

# ---------------------------------------------------------------------------
# 2. 未設定 worker は高速ループしない（backoff）・他に影響しない
# ---------------------------------------------------------------------------
if [ -n "${EXPECTED_UNCONFIGURED// /}" ]; then
  echo "== unconfigured workers: observe ${UNCONF_OBSERVE_SECONDS}s =="
  declare -A RC0=()
  for s in $EXPECTED_UNCONFIGURED; do RC0[$s]="$(restart_count "$s")"; done
  sleep "$UNCONF_OBSERVE_SECONDS"
  for s in $EXPECTED_UNCONFIGURED; do
    rc1="$(restart_count "$s")"; delta=$((rc1 - RC0[$s]))
    if [ "$delta" -le 2 ]; then
      ok "$s RestartCount ${RC0[$s]} -> $rc1 (+$delta over ${UNCONF_OBSERVE_SECONDS}s, state $(health "$s"))"
    else
      ng "$s fast-looping: RestartCount ${RC0[$s]} -> $rc1 (+$delta over ${UNCONF_OBSERVE_SECONDS}s)"
    fi
    if docker compose logs --no-log-prefix --tail=200 "$s" 2>&1 | grep -q 'backing off'; then
      ok "$s logs show controlled backoff"
    else
      ng "$s logs lack 'backing off'"
    fi
    if poller_ok "$s"; then ng "$s unexpectedly polls ${QUEUES[$s]}"; else ok "$s is not polling (unconfigured)"; fi
  done
  check_configured_healthy "with-unconfigured" 60 || true
fi

# ---------------------------------------------------------------------------
# 3. 骨組み Episode（dummy-worker: workflow → activity → completed → artifact 読み戻し）
# ---------------------------------------------------------------------------
skeleton() {
  local label="$1"
  if ./scripts/smoke.sh >"$LOG_DIR/skeleton-$label.log" 2>&1; then
    ok "[$label] skeleton episode completed ($(grep -m1 -o '"id": *"[^"]*"' "$LOG_DIR/skeleton-$label.log" || true))"
  else
    ng "[$label] skeleton smoke failed (see $LOG_DIR/skeleton-$label.log)"; tail -15 "$LOG_DIR/skeleton-$label.log" >&2
  fi
}
echo "== Phase 1 skeleton episode =="
skeleton startup

# ---------------------------------------------------------------------------
# 4. 停止スイッチ（DB）。終了時に元の状態へ戻す
# ---------------------------------------------------------------------------
switch_state() {  # $1=paused|uploads_paused → on/off
  py scripts/operational-switch.py show | sed -n "s/^$1: db=//p"
}
PREV_PAUSED="$(switch_state paused)"
PREV_UPLOADS="$(switch_state uploads_paused)"
[ -n "$PREV_PAUSED" ] && [ -n "$PREV_UPLOADS" ] || die "cannot read operational switches"
echo "switches before: paused=$PREV_PAUSED uploads_paused=$PREV_UPLOADS"
restore_switches() {
  py scripts/operational-switch.py set paused "$PREV_PAUSED" --reason "smoke-workers restore" >/dev/null || true
  py scripts/operational-switch.py set uploads_paused "$PREV_UPLOADS" --reason "smoke-workers restore" >/dev/null || true
  echo "switches restored: $(py scripts/operational-switch.py show | head -n 2 | tr '\n' ' ')"
}
trap restore_switches EXIT

echo "== DailyEpisodeWorkflow on queue pipeline with DB paused=on =="
py scripts/operational-switch.py set paused on --reason "smoke-workers" >/dev/null
[ "$(switch_state paused)" = "on" ] || die "paused switch did not turn on; refusing to start DailyEpisodeWorkflow"
slots0="$(psql_q 'select count(*) from daily_episode_slots')"; eps0="$(psql_q 'select count(*) from episodes')"
pl_started="$(started_at pipeline-worker)"
if daily="$(py scripts/smoke_workers.py daily-paused --slot-date "$SLOT_DATE" 2>"$LOG_DIR/daily.err")"; then
  echo "$daily"
  outcome="$(printf '%s' "$daily" | python3 -c 'import json,sys; print(json.load(sys.stdin)["outcome"])')"
  ep="$(printf '%s' "$daily" | python3 -c 'import json,sys; print(json.load(sys.stdin)["episode_id"])')"
  if [ "$outcome" = "paused" ] && [ "$ep" = "None" ]; then ok "daily outcome=paused, no episode"; else ng "daily outcome=$outcome episode=$ep"; fi
else
  ng "daily-paused failed"; tail -20 "$LOG_DIR/daily.err" >&2
fi
slots1="$(psql_q 'select count(*) from daily_episode_slots')"; eps1="$(psql_q 'select count(*) from episodes')"
if [ "$slots0" = "$slots1" ]; then ok "daily_episode_slots unchanged ($slots1)"; else ng "daily_episode_slots $slots0 -> $slots1"; fi
if [ "$eps0" = "$eps1" ]; then ok "episodes unchanged by daily ($eps1)"; else ng "episodes $eps0 -> $eps1"; fi
py scripts/operational-switch.py set paused "$PREV_PAUSED" --reason "smoke-workers restore" >/dev/null
[ "$(switch_state paused)" = "$PREV_PAUSED" ] && ok "paused restored to $PREV_PAUSED" || ng "paused not restored"

echo "== uploads_paused round-trip, read by the running pipeline-worker (no restart) =="
gate_probe() { py scripts/smoke_workers.py gate-probe 2>>"$LOG_DIR/gate.err"; }
py scripts/operational-switch.py set uploads_paused on --reason "smoke-workers" >/dev/null
if [ "$(switch_state uploads_paused)" = "on" ]; then ok "uploads_paused show=on after set"; else ng "uploads_paused show != on"; fi
if probe_on="$(gate_probe)"; then
  echo "$probe_on"
  if printf '%s' "$probe_on" | python3 -c '
import json,sys; r=json.load(sys.stdin)
c,g=r["check_paused_include_uploads"],r["upload_gate"]
sys.exit(0 if c["paused"] and c["reason"]=="uploads_paused (db switch)" and not g["allowed"] and g["reason"]=="uploads_paused (db switch)" else 1)'
  then ok "pipeline-worker check_paused(include_uploads)/upload_gate see uploads_paused=on"
  else ng "pipeline-worker did not report uploads_paused"; fi
else ng "gate-probe (on) failed"; tail -20 "$LOG_DIR/gate.err" >&2; fi
py scripts/operational-switch.py set uploads_paused "$PREV_UPLOADS" --reason "smoke-workers restore" >/dev/null
if [ "$(switch_state uploads_paused)" = "$PREV_UPLOADS" ]; then ok "uploads_paused restored to $PREV_UPLOADS"; else ng "uploads_paused not restored"; fi
if [ "$PREV_UPLOADS" = "off" ] && probe_off="$(gate_probe)"; then
  echo "$probe_off"
  if printf '%s' "$probe_off" | python3 -c '
import json,sys; r=json.load(sys.stdin)
c,g=r["check_paused_include_uploads"],r["upload_gate"]
sys.exit(0 if (not c["paused"]) and g["reason"]=="episode not found" else 1)'
  then ok "pipeline-worker sees uploads_paused=off again (gate falls through to 'episode not found')"
  else ng "pipeline-worker still reports paused after restore (env PAUSED/UPLOADS_PAUSED set?)"; fi
fi
if [ "$(started_at pipeline-worker)" = "$pl_started" ]; then ok "pipeline-worker was not restarted during switch checks"; else ng "pipeline-worker restarted during switch checks"; fi

# ---------------------------------------------------------------------------
# 5. 再起動シナリオ（SMOKE_RESTART=1 / SMOKE_DOWN=1）
# ---------------------------------------------------------------------------
if [ "${SMOKE_RESTART:-}" = "1" ] || [ "${SMOKE_DOWN:-}" = "1" ]; then
  # shellcheck source=scripts/smoke-workers-restart.sh
  . ./scripts/smoke-workers-restart.sh
fi

echo
docker compose ps --format 'table {{.Service}}\t{{.Status}}' | tee "$LOG_DIR/ps.txt"
if [ "$FAILS" -ne 0 ]; then
  echo "NG: $FAILS check(s) failed (logs $LOG_DIR)" >&2
  exit 1
fi
echo "OK: compose workers smoke passed"
