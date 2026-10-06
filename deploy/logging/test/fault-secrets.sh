#!/usr/bin/env bash
# S-SEC: 秘密に見える値が stdout（json-file）・Fluent Bit バッファ・OpenSearch のどこにも残らない。
#   usage: fault-secrets.sh [--stdlib-only]   （--stdlib-only は陽性対照。INV-39 の証明ではない）
# buffer に chunk を留めて走査するため、注入の間だけ OpenSearch を止める。
source "$(dirname "$0")/lib.sh"
mode="${1:-}"
state="${AVP_APPTEST_STATE:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/$AVP_LOG_TARGET_PROJECT}"
dir="$state/secrets-$(date -u +%H%M%S)"; t="$(tag secret)"; name="$AVP_LOG_TARGET_PROJECT-tool-$t"
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
# logging 経由の行（JSON 行 = log が "{" で始まる）だけを数える。--raw の print は同じ値を出すので分けて数える
echo "json-file hits(logging needles in JSON lines, 0 であること): $(grep -F '"log":"{' "$logpath" | grep -c -F -f "$dir/needles-logging.txt" || true)"
echo "json-file hits(logging needles in all lines, raw の print を含む): $(hits "$dir/needles-logging.txt" "$logpath")"
echo "json-file hits(raw needles; --raw で意図的に print した分は残る): $(hits "$dir/needles-raw.txt" "$logpath")"
step "2) Fluent Bit buffer（tmpfs）と位置 DB"
echo "chunks: $(fb_storage | tr -d '\n' | head -c 300)"
fb_fs 'echo files=$(find /proc/1/root/fb-buffer /proc/1/root/fb-state -type f | wc -l); echo buffer_hits_logging=$(grep -r -l -F -f /n/needles-logging.txt /proc/1/root/fb-buffer /proc/1/root/fb-state | wc -l); echo buffer_hits_raw=$(grep -r -l -F -f /n/needles-raw.txt /proc/1/root/fb-buffer /proc/1/root/fb-state | wc -l); i=0; while read -r n; do c=$(grep -r -l -F -e "$n" /proc/1/root/fb-buffer /proc/1/root/fb-state | wc -l); [ "$c" = 0 ] || echo "buffer hit: needle line $i in $c file(s)"; i=$((i+1)); done </n/needles-logging.txt' \
  -v "$dir:/n:ro"
step "start $os"; docker start "$os" >/dev/null
step "3) OpenSearch（_source 全走査）"
sa count --term "request_id=$t" --min 1 --wait 600
# infra 系統（print・stderr）も全部届いてから走査する（app だけ待つと、再送の backoff 中の infra を
# 走査せずに「無い」と出す。2026-10-06 実測: 34 件中 2 件しか届いていない時点で absent が ok になった）
n_json="$(grep -c -F '"log":"{' "$logpath" || true)"
OPENSEARCH_INDEX="avp-infra-test-*" sa count --term "container_name=$name" --min "$(( $(wc -l <"$logpath") - n_json ))" --wait 600
sa absent --needles-file "$dir/needles-logging.txt" --since "$since"
# infra に入るのは logging を通らない行（--raw の print・stderr）だけ。同じ値なので logging の needle でも当たり得る
# （例: PEM の print は改行で行が分かれ、BEGIN の無い本文行を Collector は見分けられない）。下の raw と合わせて報告する
OPENSEARCH_INDEX="avp-infra-test-*" sa absent --needles-file "$dir/needles-logging.txt" --min-docs 1 --since "$since" || true
OPENSEARCH_INDEX="avp-*" sa absent --needles-file "$dir/needles-raw.txt" --min-docs 1 --since "$since" || true
OPENSEARCH_INDEX="avp-*" sa count --term "redaction_applied=true" --since "$since" --min 1
echo "needles の行番号と種類: $(python3 -c "import json,sys; print(list(json.load(open(sys.argv[1]))))" "$dir/secrets.json")"
echo "needles は $dir（0600）。試験後に消す: rm -r $dir"
