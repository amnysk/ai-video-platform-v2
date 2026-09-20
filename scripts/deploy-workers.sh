#!/usr/bin/env bash
# 共通イメージを1回ビルドし、この repo からビルドする全アプリサービスを同じ版で作り直す（ADR-0024 追補）。
#
#   scripts/deploy-workers.sh
#
# 手順（どこかで失敗したら非0終了。POST_DEPLOY_CMD は成否にかかわらず必ず最後に走る）:
#   0. 作業ツリーが dirty なら拒否（ALLOW_DIRTY=1 で revision に -dirty を付けて許す）
#   1. infra（postgres / temporal / minio）が healthy か確認（作り直さない）
#   2. PRE_DEPLOY_CMD            ← フック（例: 定期実行の一時停止）
#   3. イメージをビルド（GIT_REVISION を label に焼く）  4. migrate を新イメージで実行し成功を待つ
#   5. 残りの全サービスを作り直す  6. 全サービスが healthy になるまで待つ（HEALTH_TIMEOUT 秒）
#   7. scripts/workers-versions.sh で版の揃いを確認
#   8. POST_DEPLOY_CMD           ← フック（trap で必ず実行。DEPLOY_RESULT=success|failure を渡す）
#
# フックは `bash -c` で実行する。環境変数: DEPLOY_REVISION, DEPLOY_RESULT（POST のみ）, DEPLOY_STAGE。
# このスクリプトはフックの中身を知らない。PRE が失敗したら何も変えずに中止し、POST は走る。
# POST が失敗したら、デプロイ自体が成功していても全体を失敗にする。
#
# 環境変数: ALLOW_DIRTY / HEALTH_TIMEOUT（既定 300）/ POLL_INTERVAL（既定 5）
set -euo pipefail
cd "$(dirname "$0")/.."

# デプロイ対象 = compose.yaml で build: を持つ全サービス（tests/contract/test_deploy_workers.py が一致を検査）。
# 一部だけ up -d すると、作り直されなかったコンテナが古い image id のまま動き続ける（2026-09-20 の事故）。
APP_SERVICES=(
  migrate api dummy-worker script-worker storyboard-worker production-worker
  production-image-worker production-voice-worker production-video-worker
  render-worker upload-worker pipeline-worker
)
# 同じイメージタグを共有するサービスは1つだけビルドする（app = api、worker = script-worker）
BUILD_SERVICES=(api script-worker)
INFRA_SERVICES=(postgres temporal minio)

HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-300}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
COMPOSE=(docker compose --profile core)

die() { echo "NG: $*" >&2; exit 1; }

revision="$(git rev-parse HEAD)"
if [ -n "$(git status --porcelain)" ]; then
  [ "${ALLOW_DIRTY:-0}" = "1" ] || die "作業ツリーが dirty（未コミット/未追跡の変更がある）。コミットするか ALLOW_DIRTY=1"
  revision="${revision}-dirty"
  echo "WARN: dirty なツリーからのビルド（revision=$revision）" >&2
fi
export DEPLOY_REVISION="$revision"
stage=start

# "service status health exit_code restart_policy"（health が無ければ -）
container_state() {
  docker inspect --format \
    '{{index .Config.Labels "com.docker.compose.service"}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}} {{.State.ExitCode}} {{.HostConfig.RestartPolicy.Name}}' "$1"
}

# 全サービスが「running かつ healthy（healthcheck 無しは running）」、restart: "no" は「exited 0」になるまで待つ。
wait_ready() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) pending svc cid service status health code restart
  while :; do
    pending=()
    for svc in "$@"; do
      cid="$("${COMPOSE[@]}" ps -a -q "$svc")"
      [ -n "$cid" ] || { pending+=("$svc(no container)"); continue; }
      read -r service status health code restart <<<"$(container_state "$cid")"
      if [ "$restart" = "no" ]; then
        [ "$status" = "exited" ] && [ "$code" = "0" ] && continue
        if [ "$status" = "exited" ]; then die "$svc が exit $code で終わった（docker compose logs $svc）"; fi
      else
        [ "$status" = "running" ] && { [ "$health" = "-" ] || [ "$health" = "healthy" ]; } && continue
      fi
      pending+=("$svc($status/$health)")
    done
    [ "${#pending[@]}" -eq 0 ] && return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      die "${HEALTH_TIMEOUT}秒以内に ready にならない: ${pending[*]}"
    fi
    sleep "$POLL_INTERVAL"
  done
}

# infra は作り直さないが、落ちているなら中止する（--no-deps で起動しないため）
for svc in "${INFRA_SERVICES[@]}"; do
  cid="$("${COMPOSE[@]}" ps -q "$svc")"
  [ -n "$cid" ] || die "infra $svc が起動していない"
  read -r _ status health _ <<<"$(container_state "$cid")"
  { [ "$status" = "running" ] && [ "$health" = "healthy" ]; } || die "infra $svc が healthy でない ($status/$health)"
done

post_hook() {
  local rc=$?
  trap - EXIT
  local result=failure
  [ "$rc" -eq 0 ] && result=success
  if [ -n "${POST_DEPLOY_CMD:-}" ]; then
    echo "== POST_DEPLOY_CMD (result=$result, stage=$stage)"
    if ! DEPLOY_RESULT="$result" DEPLOY_STAGE="$stage" bash -c "$POST_DEPLOY_CMD"; then
      echo "NG: POST_DEPLOY_CMD が失敗した（stage=$stage）" >&2
      [ "$rc" -eq 0 ] && rc=1
    fi
  fi
  exit "$rc"
}
trap post_hook EXIT
trap 'exit 130' INT TERM

if [ -n "${PRE_DEPLOY_CMD:-}" ]; then
  stage=pre-hook
  echo "== PRE_DEPLOY_CMD"
  DEPLOY_STAGE="$stage" bash -c "$PRE_DEPLOY_CMD" || die "PRE_DEPLOY_CMD が失敗した。何も変更していない"
fi

stage=build
echo "== build (revision=$revision)"
for svc in "${BUILD_SERVICES[@]}"; do
  "${COMPOSE[@]}" build --build-arg "GIT_REVISION=$revision" "$svc"
done

# up は --no-deps: infra を作り直さない（postgres の bind mount は相対パスで、別 worktree から
# up すると config が変わり再作成される）。APP_SERVICES の全列挙で部分適用を防ぐ。
stage=migrate
echo "== migrate"
"${COMPOSE[@]}" up -d --no-deps --no-build migrate
wait_ready migrate

stage=recreate
echo "== recreate all app services"
rest=()
for svc in "${APP_SERVICES[@]}"; do [ "$svc" = migrate ] || rest+=("$svc"); done
"${COMPOSE[@]}" up -d --no-deps --no-build "${rest[@]}"

stage=health
echo "== wait until healthy (timeout ${HEALTH_TIMEOUT}s)"
wait_ready "${APP_SERVICES[@]}"

stage=versions
echo "== verify versions"
EXPECTED_REVISION="$revision" "$(dirname "$0")/workers-versions.sh"

stage=done
echo "OK: deploy 完了 (revision=$revision)"
