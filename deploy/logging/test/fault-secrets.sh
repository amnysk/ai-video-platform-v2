#!/usr/bin/env bash
# S-SEC: 秘密に見える値が stdout（json-file）・Fluent Bit バッファ・OpenSearch のどこにも残らない。
#   usage: fault-secrets.sh [--stdlib-only]   （--stdlib-only は陽性対照。INV-39 の証明ではない）
# buffer に chunk を留めて走査するため、注入の間だけ OpenSearch を止める。
source "$(dirname "$0")/lib.sh"
mode="${1:-}"
state="${AVP_APPTEST_STATE:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/avp2-oslog-c}"
dir="$state/secrets-$(date -u +%H%M%S)"; t="$(tag secret)"; name="avp2-oslog-c-tool-$t"
since="$(date -u -d '-1 min' +%Y-%m-%dT%H:%M:%SZ)"
"$PY" "$TEST_DIR/inject-secrets.py" gen --out-dir "$dir" >/dev/null
# 隔離スタックの実パスワード（env にある秘密）も needle に足す
grep -h '_PASSWORD=' "$state/apptest.env" | cut -d= -f2 >>"$dir/needles-logging.txt"
os="$(container_of "$OPENSEARCH_SERVICE")"
step "stop $os（buffer に留める）"; docker stop "$os" >/dev/null
step "emit (tag=$t) in $name"
"$RUN_E2E" tool --secrets "$dir" --name "$name" python deploy/logging/test/inject-secrets.py \
  emit --secrets /run/avp-secrets/secrets.json --tag "$t" --raw $mode >/dev/null 2>&1 || true
sleep 15

hits() { grep -c -F -f "$1" "$2" 2>/dev/null || true; }
step "1) Docker json-file"
logpath="$(docker inspect "$name" --format '{{.LogPath}}')"
echo "lines=$(wc -l <"$logpath")"
echo "json-file hits(logging needles, 0 であること): $(hits "$dir/needles-logging.txt" "$logpath")"
echo "json-file hits(raw needles; --raw で意図的に print した分は残る): $(hits "$dir/needles-raw.txt" "$logpath")"
step "2) Fluent Bit buffer（tmpfs）と位置 DB"
echo "chunks: $(fb_storage | tr -d '\n' | head -c 300)"
fb_fs 'echo files=$(find /proc/1/root/fb-buffer /proc/1/root/fb-state -type f | wc -l); echo buffer_hits_logging=$(grep -r -l -F -f /n/needles-logging.txt /proc/1/root/fb-buffer /proc/1/root/fb-state | wc -l); echo buffer_hits_raw=$(grep -r -l -F -f /n/needles-raw.txt /proc/1/root/fb-buffer /proc/1/root/fb-state | wc -l)' \
  -v "$dir:/n:ro"
step "start $os"; docker start "$os" >/dev/null
step "3) OpenSearch（_source 全走査）"
sa count --term "request_id=$t" --min 1 --wait 600
sa absent --needles-file "$dir/needles-logging.txt" --since "$since"
OPENSEARCH_INDEX="avp-infra-test-*" sa absent --needles-file "$dir/needles-logging.txt" --min-docs 0 --since "$since"
OPENSEARCH_INDEX="avp-*" sa absent --needles-file "$dir/needles-raw.txt" --min-docs 0 --since "$since" || true
OPENSEARCH_INDEX="avp-*" sa count --term "redaction_applied=true" --since "$since" --min 1
echo "needles は $dir（0600）。試験後に消す: rm -r $dir"
