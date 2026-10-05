#!/usr/bin/env bash
# ログ基盤の確認（ADR-0040 §5/§7）。OpenSearch の**外**から、ホストで動かす。異常があれば非0で終了する。
#
#   deploy/logging/scripts/check-pipeline.sh [--env prod] [--project avp2-logging]
#       [--secrets-dir DIR] [--os-url URL] [--max-lag-min 60] [--max-lag-infra-min 60]
#       [--max-chunks 2000]
#       [--state-dir DIR] [--enforce-memory]
#
# 見るもの:
#   Fluent Bit  health、開いたファイル数（0 = containers/ が読めていない。無音の失敗）、破棄・再送失敗・
#               長すぎる行の skip（前回からの増分）、filesystem buffer の chunk 数
#   OpenSearch  到達性・cluster の状態、系統（app / infra）ごとの最終 ingested_at と lag、
#               index の env と文書の environment の食い違い（直近24時間）、index サイズ、ディスク使用率
#   証明書      CA・ノード証明書の残り日数（30日未満で異常）
#   ホスト      Docker root のディスク、MemAvailable（ADR-0040 §7 の閾値）
#
# --enforce-memory: MemAvailable が 2GiB 未満なら Dashboards、1.5GiB 未満なら OpenSearch を止める。
#   止めるのは --project のコンテナだけ（アプリの project には触れない）。既定は報告だけ。
#
# 資格情報: 読み取り専用ユーザー avp_viewer のパスワード <secrets-dir>/viewer.pw と CA <secrets-dir>/pki/ca.pem
#   （init-secrets.sh が作る。0600）。admin 証明書は使わない。パスワードは curl の argv に出さず、
#   0600 の一時 config（curl -K）で渡す。
# 「ログが無い」で未実行と判断しない。lag の異常は収集停止・buffer 待ち・業務が動いていない、を区別できない。
set -uo pipefail

ENV_NAME="${AVP_LOGGING_ENV:-prod}"
PROJECT="avp2-logging"
SECRETS_DIR=""
OS_URL="https://127.0.0.1:${AVP_LOGGING_OS_PORT:-9200}"
MAX_LAG_MIN=60
MAX_LAG_INFRA_MIN=60
MAX_CHUNKS=2000
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/avp-logging"
ENFORCE=0
MEM_STOP_DASHBOARDS_KB=$((2 * 1024 * 1024))
MEM_STOP_OPENSEARCH_KB=$((1536 * 1024))
CERT_MIN_DAYS=30
DISK_MAX_PCT=90
OS_IMAGE="opensearchproject/opensearch:3.8.0@sha256:fafe3fc3587088674669235575aa166228c48bdb940294a8cdbbc1da75236a40"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2 ;;
    --project) PROJECT="$2"; shift 2 ;;
    --secrets-dir) SECRETS_DIR="$2"; shift 2 ;;
    --os-url) OS_URL="$2"; shift 2 ;;
    --max-lag-min) MAX_LAG_MIN="$2"; shift 2 ;;
    --max-lag-infra-min) MAX_LAG_INFRA_MIN="$2"; shift 2 ;;
    --max-chunks) MAX_CHUNKS="$2"; shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --enforce-memory) ENFORCE=1; shift ;;
    -h|--help) sed -n '2,22p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
SECRETS_DIR="${SECRETS_DIR:-${AVP_LOGGING_SECRETS_DIR:-$HOME/.config/avp-logging/$ENV_NAME}}"
CA="$SECRETS_DIR/pki/ca.pem"
VIEWER_PW_FILE="$SECRETS_DIR/viewer.pw"

FAILS=0
ok() { echo "OK    $*"; }
warn() { echo "WARN  $*"; }
fail() { echo "FAIL  $*"; FAILS=$((FAILS + 1)); }

# アプリの project を止める誤用を拒む
case "$PROJECT" in
  avp2|"${AVP_LOG_TARGET_PROJECT:-avp2}")
    echo "refusing: --project $PROJECT はアプリの project" >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
STATE_FILE="$STATE_DIR/check-$PROJECT-$ENV_NAME.state"

# ---------------------------------------------------------------- Fluent Bit
fb_get() {
  # Fluent Bit は外への経路の無い internal network にだけいる。同じ network の one-shot から読む
  docker run --rm --network "${PROJECT}_internal" --entrypoint curl "$OS_IMAGE" \
    -s --max-time 10 "http://fluent-bit:2020$1" 2>/dev/null
}
metric_sum() {
  # Prometheus のテキストから、名前が一致する系列の合計
  awk -v n="$1" '$1 ~ "^"n"({|$)" { s += $2 } END { printf "%d\n", s + 0 }' <<<"$2"
}

health="$(fb_get /api/v2/health)"
if [[ -z "$health" ]]; then
  fail "fluent-bit: 到達できない（network ${PROJECT}_internal / コンテナを確認）"
else
  if grep -q '"status":"ok"' <<<"$health"; then ok "fluent-bit health: $health"; else fail "fluent-bit health: $health"; fi
  metrics="$(fb_get /api/v1/metrics/prometheus)"
  opened="$(metric_sum fluentbit_input_files_opened_total "$metrics")"
  dropped="$(metric_sum fluentbit_output_dropped_records_total "$metrics")"
  rfailed="$(metric_sum fluentbit_output_retries_failed_total "$metrics")"
  retries="$(metric_sum fluentbit_output_retries_total "$metrics")"
  skipped="$(metric_sum fluentbit_input_long_line_skipped_total "$metrics")"
  if [[ "$opened" -eq 0 ]]; then
    fail "fluent-bit: 開いたファイルが 0（containers/ が読めていない・mount 不成立。無音になる失敗）"
  else
    ok "fluent-bit: files_opened=$opened retries=$retries"
  fi
  prev_dropped=0 prev_rfailed=0 prev_skipped=0
  # shellcheck disable=SC1090
  [[ -f "$STATE_FILE" ]] && source "$STATE_FILE"
  # 再起動でカウンタが戻ったら 0 から数え直す
  [[ "$dropped" -lt "$prev_dropped" ]] && prev_dropped=0
  [[ "$rfailed" -lt "$prev_rfailed" ]] && prev_rfailed=0
  [[ "$skipped" -lt "$prev_skipped" ]] && prev_skipped=0
  d_dropped=$((dropped - prev_dropped)) d_rfailed=$((rfailed - prev_rfailed)) d_skipped=$((skipped - prev_skipped))
  if [[ $d_dropped -gt 0 || $d_rfailed -gt 0 ]]; then
    fail "fluent-bit: 破棄 +$d_dropped records / 再送上限超過 +$d_rfailed chunks（前回から。累計 $dropped / $rfailed）"
  else
    ok "fluent-bit: 破棄なし（累計 dropped=$dropped retries_failed=$rfailed）"
  fi
  if [[ $d_skipped -gt 0 ]]; then
    fail "fluent-bit: buffer_max_size を超える行の skip +$d_skipped（累計 $skipped）"
  fi
  printf 'prev_dropped=%d\nprev_rfailed=%d\nprev_skipped=%d\n' "$dropped" "$rfailed" "$skipped" >"$STATE_FILE"
  storage="$(fb_get /api/v1/storage)"
  chunks="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["storage_layer"]["chunks"]["total_chunks"])' <<<"$storage" 2>/dev/null || echo -1)"
  if [[ "$chunks" -lt 0 ]]; then
    fail "fluent-bit: storage を読めない"
  elif [[ "$chunks" -gt "$MAX_CHUNKS" ]]; then
    fail "fluent-bit: buffer の chunk が $chunks（> $MAX_CHUNKS。OpenSearch へ送れていない）"
  else
    ok "fluent-bit: buffer chunks=$chunks"
  fi
fi

# ---------------------------------------------------------------- OpenSearch
# パスワードを argv（ps で見える）に出さない。0600 の一時 config を curl -K で読ませる
CURL_CFG=""
cleanup() { [[ -n "$CURL_CFG" ]] && rm -f "$CURL_CFG"; }
trap cleanup EXIT
os_get() {
  curl -s --max-time 15 --cacert "$CA" -K "$CURL_CFG" \
    -H 'Content-Type: application/json' "$OS_URL$1" "${@:2}"
}
# 系統ごとの最終 ingested_at からの経過（分）。-1 = 文書なし、-2 = 読めない
lag_of() {
  os_get "/avp-$1-$ENV_NAME-*/_search" -X POST \
    -d '{"size":0,"aggs":{"last":{"max":{"field":"ingested_at"}}}}' | python3 -c '
import json, sys, time
v = json.load(sys.stdin)["aggregations"]["last"]["value"]
print(-1 if v is None else int((time.time() * 1000 - v) / 60000))' 2>/dev/null || echo -2
}
check_lag() {
  local series="$1" max="$2" lag
  lag="$(lag_of "$series")"
  if [[ "$lag" -eq -2 ]]; then
    fail "opensearch[$series]: 最終 ingested_at を読めない"
  elif [[ "$lag" -eq -1 ]]; then
    fail "opensearch[$series]: 文書が1件も無い"
  elif [[ "$lag" -gt "$max" ]]; then
    fail "opensearch[$series]: 最終取り込みから ${lag} 分（> ${max}。収集停止・buffer 待ち・業務停止を区別できない。DB・Temporal と照合）"
  else
    ok "opensearch[$series]: 最終取り込みから ${lag} 分"
  fi
}
if [[ ! -r "$VIEWER_PW_FILE" || ! -r "$CA" ]]; then
  fail "資格情報が読めない: $VIEWER_PW_FILE / $CA"
else
  CURL_CFG="$(mktemp)"
  chmod 600 "$CURL_CFG"
  printf 'user = "avp_viewer:%s"\n' "$(cat "$VIEWER_PW_FILE")" >"$CURL_CFG"
  status="$(os_get '/_cluster/health' | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null)"
  case "$status" in
    green|yellow) ok "opensearch: cluster $status" ;;
    "") fail "opensearch: 到達できない（$OS_URL）" ;;
    *) fail "opensearch: cluster $status" ;;
  esac
  if [[ -n "$status" ]]; then
    # app と infra を別に見る（temporal 等の infra の行で app の停止が隠れないように。I-6）
    check_lag app "$MAX_LAG_MIN"
    check_lag infra "$MAX_LAG_INFRA_MIN"
    # index の env（Collector の AVP_LOGGING_ENV）と文書の environment（アプリの AVP_ENVIRONMENT）の
    # 食い違い。本番の .env に AVP_ENVIRONMENT=prod が無いと unknown 等で入る（I-5）
    mismatch="$(os_get "/avp-app-$ENV_NAME-*/_count" -X POST -d "{\"query\":{\"bool\":{
      \"filter\":[{\"range\":{\"ingested_at\":{\"gte\":\"now-24h\"}}}],
      \"must_not\":[{\"term\":{\"environment\":\"$ENV_NAME\"}}]}}}" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' 2>/dev/null || echo -1)"
    if [[ "$mismatch" -lt 0 ]]; then
      fail "opensearch[app]: environment の食い違いを数えられない"
    elif [[ "$mismatch" -gt 0 ]]; then
      fail "opensearch[app]: 直近24時間に environment≠$ENV_NAME の文書が $mismatch 件（アプリの .env の AVP_ENVIRONMENT を確認）"
    else
      ok "opensearch[app]: 直近24時間の environment はすべて $ENV_NAME"
    fi
    os_get "/_cat/indices/avp-*-$ENV_NAME-*?h=index,docs.count,store.size&s=index" | sed 's/^/      /'
    disk="$(os_get '/_cat/allocation?h=disk.percent' | tr -dc '0-9\n' | head -n1)"
    if [[ -n "$disk" && "$disk" -ge 85 ]]; then fail "opensearch: ディスク ${disk}%"; else ok "opensearch: ディスク ${disk:-?}%"; fi
  fi
fi

# ---------------------------------------------------------------- 証明書
for cert in "$SECRETS_DIR/pki/ca.pem" "$SECRETS_DIR/pki/node.pem"; do
  if [[ ! -r "$cert" ]]; then
    fail "証明書が読めない: $cert"
  elif openssl x509 -in "$cert" -noout -checkend $((CERT_MIN_DAYS * 86400)) >/dev/null; then
    ok "証明書 $(basename "$cert"): $(openssl x509 -in "$cert" -noout -enddate | cut -d= -f2) まで"
  else
    fail "証明書 $(basename "$cert") の期限が ${CERT_MIN_DAYS} 日未満（init-secrets.sh --rotate-node-cert）"
  fi
done

# ---------------------------------------------------------------- ホスト
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)"
if [[ -n "$docker_root" ]]; then
  pct="$(df -P "$docker_root" | awk 'NR==2 { gsub("%", "", $5); print $5 }')"
  if [[ "$pct" -ge "$DISK_MAX_PCT" ]]; then fail "host: Docker root のディスク ${pct}%"; else ok "host: Docker root のディスク ${pct}%"; fi
fi
avail_kb="$(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo)"
avail_mib=$((avail_kb / 1024))
stop_service() {
  local ids
  ids="$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT" \
    --filter "label=com.docker.compose.service=$1")"
  if [[ -n "$ids" ]]; then
    # shellcheck disable=SC2086
    docker stop $ids >/dev/null && echo "      stopped $PROJECT/$1"
  fi
}
if [[ "$avail_kb" -lt "$MEM_STOP_OPENSEARCH_KB" ]]; then
  fail "host: MemAvailable ${avail_mib}MiB（< 1.5GiB。OpenSearch を止める閾値）"
  if [[ $ENFORCE -eq 1 ]]; then stop_service dashboards; stop_service opensearch; fi
elif [[ "$avail_kb" -lt "$MEM_STOP_DASHBOARDS_KB" ]]; then
  fail "host: MemAvailable ${avail_mib}MiB（< 2GiB。Dashboards を止める閾値）"
  if [[ $ENFORCE -eq 1 ]]; then stop_service dashboards; fi
else
  ok "host: MemAvailable ${avail_mib}MiB"
fi

if [[ $FAILS -gt 0 ]]; then
  echo "result: $FAILS 件の異常"
  exit 1
fi
echo "result: ok"
