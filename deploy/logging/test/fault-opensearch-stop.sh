#!/usr/bin/env bash
# S-FAULT-OS: OpenSearch 停止中もアプリ側（loggen）は止まらず、再開後に欠損・重複なく追いつく。
#   usage: fault-opensearch-stop.sh [停止秒数=120] [行数=3000]
source "$(dirname "$0")/lib.sh"
down_s="${1:-120}"; n="${2:-3000}"; t="$(tag osstop)"
os="$(container_of "$OPENSEARCH_SERVICE")"; [ -n "$os" ] || { echo "OpenSearch コンテナが無い" >&2; exit 2; }

step "before: fluent-bit retry/drop metrics"; fb_metrics 'retr|drop|storage'
step "stop $os"; docker stop "$os" >/dev/null
start=$(date +%s)
step "emit $n lines (tag=$t) while OpenSearch is down"
"$RUN_E2E" tool python deploy/logging/test/loggen.py --tag "$t" --count "$n" --rate 50 >/dev/null
echo "loggen elapsed: $(( $(date +%s) - start ))s（アプリ側が止まらないこと）"
elapsed=$(( $(date +%s) - start ))
[ "$elapsed" -ge "$down_s" ] || sleep "$(( down_s - elapsed ))"
step "during outage metrics"; fb_metrics 'retr|drop|storage'
step "start $os"; docker start "$os" >/dev/null
sa count --term "request_id=$t" --expect "$n" --wait 900
sa dupes --term "request_id=$t" --expect 0
step "after metrics"; fb_metrics 'retr|drop|storage'
