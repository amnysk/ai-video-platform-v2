#!/usr/bin/env bash
# S-SEC: 秘密に見える値が stdout（json-file）・Fluent Bit バッファ・OpenSearch のどこにも残らない。
#   usage: fault-secrets.sh [--stdlib-only]   （--stdlib-only は A 実装前の経路確認。INV-39 の証明ではない）
source "$(dirname "$0")/lib.sh"
mode="${1:-}"
state="${AVP_APPTEST_STATE:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/avp2-oslog-c}"
dir="$state/secrets-$(date -u +%H%M%S)"; t="$(tag secret)"; name="avp2-oslog-c-tool-$t"
"$PY" "$TEST_DIR/inject-secrets.py" gen --out-dir "$dir" >/dev/null
# 隔離スタックの実パスワードも needle に足す（env にある秘密が漏れないこと）
grep -h '_PASSWORD=' "$state/apptest.env" | cut -d= -f2 >>"$dir/needles-logging.txt"
step "emit (tag=$t) in $name"
"$RUN_E2E" tool --secrets "$dir" --name "$name" python deploy/logging/test/inject-secrets.py \
  emit --secrets /run/avp-secrets/secrets.json --tag "$t" --raw $mode >/dev/null 2>&1 || true

hits() { grep -c -F -f "$1" "$2" 2>/dev/null || true; }
step "1) Docker json-file（logging 経由の値は 0 件であること）"
logpath="$(docker inspect "$name" --format '{{.LogPath}}')"
echo "json-file hits(logging needles): $(hits "$dir/needles-logging.txt" "$logpath")"
echo "json-file hits(raw needles; --raw で意図的に出した分は残る): $(hits "$dir/needles-raw.txt" "$logpath")"
step "2) Fluent Bit filesystem buffer（読み取り専用で走査）"
docker run --rm --network none -v "$FLUENTBIT_STORAGE_VOLUME:/b:ro" -v "$dir:/n:ro" busybox:1.36 \
  sh -c 'grep -r -l -F -f /n/needles-logging.txt /b | wc -l' || echo "buffer volume が無い（B の命名に合わせる）"
step "3) OpenSearch（_source 全走査。attributes は検索対象外なので query では探せない）"
sa count --term "request_id=$t" --min 1 --wait 300 || true
sa absent --needles-file "$dir/needles-logging.txt" --since "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)"
OPENSEARCH_INDEX="${OPENSEARCH_INFRA_INDEX:-avp-infra-test-*}" \
  sa absent --needles-file "$dir/needles-raw.txt" --min-docs 0 \
  --since "$(date -u -d '-1 hour' +%Y-%m-%dT%H:%M:%SZ)"
echo "needles は $dir（0600）。試験後に消す: rm -r $dir"
