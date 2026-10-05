# 障害注入スクリプトの共通部（source して使う）。B の compose.logging.yaml に合わせて env で差し替える。
#
#   LOGGING_PROJECT         ログ基盤の compose project（既定 avp2-oslog-c-log。本番の avp2-logging は拒否）
#                           隔離 app スタック（avp2-oslog-c）とは別 project にする（down -v を分けるため）
#   AVP_LOGGING_*           B の compose.logging.yaml + compose.test.yaml の env（logging-env.sh が既定値を置く）
#   OPENSEARCH_SERVICE      compose のサービス名（既定 opensearch）
#   FLUENTBIT_SERVICE       compose のサービス名（既定 fluent-bit）
#   OPENSEARCH_URL / OPENSEARCH_CA / OPENSEARCH_USER / OPENSEARCH_PASSWORD_FILE / OPENSEARCH_INDEX
#                           search-assert.py が読む（既定は B の platform.md §4 の置き場所）
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$TEST_DIR/../../.." && pwd)"
LOGGING_PROJECT="${LOGGING_PROJECT:-avp2-oslog-c-log}"
case "$LOGGING_PROJECT" in
  avp2|avp2-logging) echo "NG: LOGGING_PROJECT=$LOGGING_PROJECT は本番。試験では使わない" >&2; exit 2 ;;
esac
# B の隔離試験の env（platform.md §9）。呼び出し側で上書きできる
export AVP_LOGGING_ENV="${AVP_LOGGING_ENV:-test}"
export AVP_LOGGING_SECRETS_DIR="${AVP_LOGGING_SECRETS_DIR:-$HOME/.config/avp-logging-test/c}"
export AVP_LOG_TARGET_PROJECT="${AVP_LOG_TARGET_PROJECT:-${AVP_APPTEST_PROJECT:-avp2-oslog-c}}"
export AVP_LOG_CONTAINERS_DIR="${AVP_LOG_CONTAINERS_DIR:-$HOME/.local/share/docker/containers}"
export AVP_LOGGING_OS_PORT="${AVP_LOGGING_OS_PORT:-19203}" AVP_LOGGING_OSD_PORT="${AVP_LOGGING_OSD_PORT:-15604}"
export AVP_LOGGING_OS_HEAP="${AVP_LOGGING_OS_HEAP:-512m}" AVP_LOGGING_OS_MEM_LIMIT="${AVP_LOGGING_OS_MEM_LIMIT:-1400m}"
export AVP_LOG_BUFFER_TMPFS_SIZE="${AVP_LOG_BUFFER_TMPFS_SIZE:-64m}" AVP_LOG_STORAGE_LIMIT="${AVP_LOG_STORAGE_LIMIT:-48M}"
export AVP_ISM_JOB_INTERVAL_MIN="${AVP_ISM_JOB_INTERVAL_MIN:-1}" AVP_LOG_HOST_NAME="${AVP_LOG_HOST_NAME:-$(hostname)}"
export OPENSEARCH_URL="${OPENSEARCH_URL:-https://127.0.0.1:$AVP_LOGGING_OS_PORT}"
export OPENSEARCH_CA="${OPENSEARCH_CA:-$AVP_LOGGING_SECRETS_DIR/pki/ca.pem}"
export OPENSEARCH_USER="${OPENSEARCH_USER:-avp_viewer}"
export OPENSEARCH_PASSWORD_FILE="${OPENSEARCH_PASSWORD_FILE:-$AVP_LOGGING_SECRETS_DIR/viewer.pw}"
export OPENSEARCH_INDEX="${OPENSEARCH_INDEX:-avp-app-test-*}"
LOGGING_COMPOSE="${LOGGING_COMPOSE:-$SRC/deploy/logging/compose.logging.yaml}"
LOGGING_COMPOSE_TEST="${LOGGING_COMPOSE_TEST:-$SRC/deploy/logging/compose.test.yaml}"
OPENSEARCH_SERVICE="${OPENSEARCH_SERVICE:-opensearch}"
FLUENTBIT_SERVICE="${FLUENTBIT_SERVICE:-fluent-bit}"
FLUENTBIT_STATE_VOLUME="${FLUENTBIT_STATE_VOLUME:-${LOGGING_PROJECT}_fbstate}"
PY="${PY:-$SRC/.venv/bin/python}"
RUN_E2E="$TEST_DIR/run-e2e.sh"

ldc() {
  docker compose -p "$LOGGING_PROJECT" -f "$LOGGING_COMPOSE" -f "$LOGGING_COMPOSE_TEST" "$@"
}

container_of() {  # compose サービス名 -> コンテナ名（ログ基盤 project 内）
  docker ps -a --filter "label=com.docker.compose.project=$LOGGING_PROJECT" \
    --filter "label=com.docker.compose.service=$1" --format '{{.Names}}' | head -n1
}

sa() { "$PY" "$TEST_DIR/search-assert.py" "$@"; }

tag() { echo "$1-$(date -u +%H%M%S)-$RANDOM"; }

fb_metrics() {  # Fluent Bit の Prometheus 形式 metrics から、名前に一致する行だけ（B の fb-metrics.sh 経由）
  "$SRC/deploy/logging/scripts/fb-metrics.sh" --project "$LOGGING_PROJECT" /api/v1/metrics/prometheus \
    | grep -E "${1:-.}" | grep -v '^#' || true
}

fb_storage() { "$SRC/deploy/logging/scripts/fb-metrics.sh" --project "$LOGGING_PROJECT" /api/v1/storage; }

step() { printf '\n== %s\n' "$*"; }

sa_admin() {  # alias / ISM / template の読み取り（viewer は 403）。admin 証明書で GET だけ行う
  OPENSEARCH_CLIENT_CERT="$AVP_LOGGING_SECRETS_DIR/pki/admin.pem" \
  OPENSEARCH_CLIENT_KEY="$AVP_LOGGING_SECRETS_DIR/pki/admin.key" sa "$@"
}

fb_fs() {  # Fluent Bit（distroless・tmpfs の buffer）のファイルを、pid namespace を共有した一時コンテナから読む
  local fb; fb="$(container_of "$FLUENTBIT_SERVICE")"
  docker run --rm --pid "container:$fb" --network none --user 0:0 "${@:2}" busybox:1.36 sh -c "$1"
}
