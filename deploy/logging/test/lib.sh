# 障害注入スクリプトの共通部（source して使う）。B の compose.logging.yaml に合わせて env で差し替える。
#
#   LOGGING_PROJECT         ログ基盤の compose project（既定 avp2-logging-c。本番の avp2-logging は拒否）
#   LOGGING_COMPOSE         B の compose ファイル（既定 deploy/logging/compose.logging.yaml）
#   LOGGING_ENV_FILE        その env ファイル（任意）
#   OPENSEARCH_SERVICE      compose のサービス名（既定 opensearch）
#   FLUENTBIT_SERVICE       compose のサービス名（既定 fluent-bit）
#   FLUENTBIT_METRICS_URL   既定 http://127.0.0.1:2020
#   FLUENTBIT_STORAGE_VOLUME  filesystem buffer の volume 名（既定 ${LOGGING_PROJECT}_fluentbit-storage）
#   OPENSEARCH_URL / OPENSEARCH_CA / OPENSEARCH_USER / OPENSEARCH_PASSWORD_FILE / OPENSEARCH_INDEX
#                           search-assert.py が読む
set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$(cd "$TEST_DIR/../../.." && pwd)"
LOGGING_PROJECT="${LOGGING_PROJECT:-avp2-logging-c}"
case "$LOGGING_PROJECT" in
  avp2|avp2-logging) echo "NG: LOGGING_PROJECT=$LOGGING_PROJECT は本番。試験では使わない" >&2; exit 2 ;;
esac
LOGGING_COMPOSE="${LOGGING_COMPOSE:-$SRC/deploy/logging/compose.logging.yaml}"
OPENSEARCH_SERVICE="${OPENSEARCH_SERVICE:-opensearch}"
FLUENTBIT_SERVICE="${FLUENTBIT_SERVICE:-fluent-bit}"
FLUENTBIT_METRICS_URL="${FLUENTBIT_METRICS_URL:-http://127.0.0.1:2020}"
FLUENTBIT_STORAGE_VOLUME="${FLUENTBIT_STORAGE_VOLUME:-${LOGGING_PROJECT}_fluentbit-storage}"
PY="${PY:-$SRC/.venv/bin/python}"
RUN_E2E="$TEST_DIR/run-e2e.sh"

ldc() {
  local extra=()
  [ -n "${LOGGING_ENV_FILE:-}" ] && extra=(--env-file "$LOGGING_ENV_FILE")
  docker compose -p "$LOGGING_PROJECT" "${extra[@]}" -f "$LOGGING_COMPOSE" "$@"
}

container_of() {  # compose サービス名 -> コンテナ名（ログ基盤 project 内）
  docker ps -a --filter "label=com.docker.compose.project=$LOGGING_PROJECT" \
    --filter "label=com.docker.compose.service=$1" --format '{{.Names}}' | head -n1
}

sa() { "$PY" "$TEST_DIR/search-assert.py" "$@"; }

tag() { echo "$1-$(date -u +%H%M%S)-$RANDOM"; }

fb_metrics() {  # Fluent Bit の Prometheus 形式 metrics から、名前に一致する行だけ
  curl -fsS "$FLUENTBIT_METRICS_URL/api/v2/metrics/prometheus" | grep -E "${1:-.}" | grep -v '^#' || true
}

step() { printf '\n== %s\n' "$*"; }
