#!/usr/bin/env bash
# 隔離 app スタック（compose.apptest.yaml）の起動・pytest 実行・片付け。
#
#   run-e2e.sh build            worker イメージ（この worktree の Dockerfile）と test-runner を作る
#   run-e2e.sh up               postgres / temporal / minio / namespace / migrate を用意する
#   run-e2e.sh run [args...]    test-runner で pytest（既定: tests/integration 全部）
#   run-e2e.sh run-each [files...]
#                               integration の各ファイルを別コンテナ（container_name=<project>-r-<stem>）で
#                               実行する。OpenSearch で container_name によりテストとログを対応づける
#   run-e2e.sh tool [--secrets DIR] [--name N] cmd...
#                               一時コンテナ（label avp.logging=app）で cmd を実行（loggen 等）
#   run-e2e.sh ps | logs        状態・ログ
#   run-e2e.sh stop             コンテナを止める（volume・パスワードは残す。up で再開）
#   run-e2e.sh down             コンテナ・network・volume を消す（この project だけ）
#
# 本番（compose project `avp2`）には触れない。project 名が avp2 なら拒否する。
# 秘密（DB / MinIO のパスワード）は実行時に生成し、repo 外の状態ディレクトリ（0700/0600）に置く。
#
# 主な env:
#   AVP_APPTEST_PROJECT   compose project 名（既定 avp2-oslog-c。`avp2-oslog-c*` のみ許可）
#   AVP_APPTEST_STATE     状態ディレクトリ（既定 ${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/<project>）
#   APPTEST_RUNNER_NAME   run の container 名（既定 <project>-runner-<UTC時刻>）。Collector 追いつき
#                         確認まで残すため --rm しない。`down` で消える
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$HERE/../../.." && pwd)"
PROJECT="${AVP_APPTEST_PROJECT:-avp2-oslog-c}"
case "$PROJECT" in
  avp2-oslog-c|avp2-oslog-c[-_0-9a-z]*) ;;
  *) echo "NG: AVP_APPTEST_PROJECT=$PROJECT は許可しない（avp2-oslog-c* のみ。本番 avp2 を守る）" >&2; exit 2 ;;
esac
STATE="${AVP_APPTEST_STATE:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/$PROJECT}"
ENV_FILE="$STATE/apptest.env"
WORKER_IMAGE="${APPTEST_WORKER_IMAGE:-avp2-oslog-c-worker:test}"
RUNNER_IMAGE="${APPTEST_RUNNER_IMAGE:-avp2-oslog-c-runner:test}"
case "$WORKER_IMAGE $RUNNER_IMAGE" in
  *avp2-worker:local*|*avp2-app:local*) echo "NG: 本番のイメージ tag を上書きしない" >&2; exit 2 ;;
esac

# .env があると Settings() が読む（FAL_KEY 等が混入し得る）。mount するツリーに置かせない
if [ -e "$SRC/.env" ]; then
  echo "NG: $SRC/.env が存在する。隔離試験では .env を置かない（秘密の混入防止）" >&2
  exit 2
fi

ensure_secrets() {
  install -d -m 700 "$STATE"
  if [ ! -s "$ENV_FILE" ]; then
    umask 077
    {
      echo "APPTEST_PG_PASSWORD=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
      echo "APPTEST_MINIO_PASSWORD=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    } >"$ENV_FILE"
  fi
}

dc() {
  # tool サービスの secrets mount 元（compose は全サービスを補間するので常に渡す）
  install -d -m 700 "$STATE/empty"
  APPTEST_SECRETS_DIR="${APPTEST_SECRETS_DIR:-$STATE/empty}" \
  APPTEST_SRC="$SRC" \
  APPTEST_RUNNER_IMAGE="$RUNNER_IMAGE" \
  APPTEST_GIT_REVISION="$(git -C "$SRC" rev-parse HEAD 2>/dev/null || echo unknown)" \
  AVP_APPTEST_PROJECT="$PROJECT" \
    docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$HERE/compose.apptest.yaml" "$@"
}

cmd="${1:-help}"
shift || true
case "$cmd" in
  build)
    rev="$(git -C "$SRC" rev-parse HEAD)"
    docker build --target worker --build-arg GIT_REVISION="$rev" -t "$WORKER_IMAGE" "$SRC"
    docker build --build-arg BASE_IMAGE="$WORKER_IMAGE" -t "$RUNNER_IMAGE" \
      -f "$HERE/Dockerfile.test-runner" "$HERE"
    ;;
  up)
    ensure_secrets
    start=$(date +%s)
    dc up -d --wait postgres minio temporal
    dc up --exit-code-from temporal-namespace temporal-namespace
    dc up --exit-code-from migrate migrate
    echo "up: $(( $(date +%s) - start ))s"
    ;;
  run)
    ensure_secrets
    name="${APPTEST_RUNNER_NAME:-$PROJECT-runner-$(date -u +%Y%m%dT%H%M%SZ)}"
    start=$(date +%s)
    set +e
    if [ "$#" -gt 0 ]; then
      dc run -T --name "$name" test-runner "$@"
    else
      dc run -T --name "$name" test-runner
    fi
    rc=$?
    set -e
    echo "run: container=$name rc=$rc elapsed=$(( $(date +%s) - start ))s"
    exit "$rc"
    ;;
  run-each)
    ensure_secrets
    files=("$@")
    [ "${#files[@]}" -gt 0 ] || mapfile -t files < <(cd "$SRC" && ls tests/integration/test_*.py)
    summary="$STATE/run-each-$(date -u +%Y%m%dT%H%M%SZ).tsv"
    total_start=$(date +%s)
    for f in "${files[@]}"; do
      stem="$(basename "$f" .py)"; stem="${stem#test_}"
      name="$PROJECT-r-${stem//_/-}"
      docker rm -f "$name" >/dev/null 2>&1 || true
      start=$(date +%s)
      out="$(dc run -T --name "$name" test-runner pytest "$f" -p no:cacheprovider \
        -p tests.support.json_log_plugin -q -rs 2>&1 | grep -E '^[0-9]+ (passed|failed|skipped|error)|^(FAILED|ERROR|SKIPPED) ' | tr '\n' ' ')"
      printf '%s\t%s\t%ss\t%s\n' "$f" "$name" "$(( $(date +%s) - start ))" "$out" | tee -a "$summary"
    done
    echo "run-each: $(( $(date +%s) - total_start ))s summary=$summary"
    ;;
  tool)
    ensure_secrets
    secrets_dir=""
    name="$PROJECT-tool-$(date -u +%Y%m%dT%H%M%SZ)"
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --secrets) secrets_dir="$(cd "$2" && pwd)"; shift 2 ;;
        --name) name="$2"; shift 2 ;;
        *) break ;;
      esac
    done
    [ "$#" -gt 0 ] || { echo "tool: コマンドを指定する" >&2; exit 2; }
    user_opt=()
    if [ -n "$secrets_dir" ]; then
      # secrets は host 所有者の 0600。rootless Docker では host の所有者＝コンテナ内 uid 0 なので、
      # 読むには uid 0 で動かす（cap は全て落としてあり、host 上は非特権）。rootful では
      # APPTEST_TOOL_USER に host 所有者の uid:gid を渡す
      user_opt=(--user "${APPTEST_TOOL_USER:-0:0}")
    fi
    APPTEST_SECRETS_DIR="${secrets_dir:-$STATE/empty}" \
      dc run -T --no-deps "${user_opt[@]}" --name "$name" tool "$@"
    echo "tool: container=$name"
    ;;
  ps) ensure_secrets; dc ps -a ;;
  logs) ensure_secrets; dc logs "$@" ;;
  stop) ensure_secrets; dc --profile runner --profile tools stop ;;
  down)
    ensure_secrets
    dc --profile runner --profile tools down -v --remove-orphans
    # run で作った one-off コンテナ（--rm しない）も project label で消す
    ids="$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")"
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
    rm -f "$ENV_FILE"
    ;;
  *)
    sed -n '2,20p' "$0"
    ;;
esac
