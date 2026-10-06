#!/usr/bin/env bash
# S-BAD: 不正 JSON・型不整合・@timestamp 不正・未知キー・長大行が混ざっても、同じ chunk の正常行は
# 取り込まれ（Bulk 部分失敗で詰まらない）、型不整合は collector_errors / attributes.collector_moved に残る。
#   usage: fault-bad-lines.sh [valid 行数=500] [長大行 bytes=40000]
source "$(dirname "$0")/lib.sh"
n="${1:-500}"; long="${2:-40000}"; t="$(tag bad)"
step "emit $n valid + malformed + 1 long line ($long B) tag=$t"
"$RUN_E2E" tool python deploy/logging/test/loggen.py --tag "$t" --count "$n" --malformed --long-line "$long" >/dev/null
# 正常行は全件（長大行は skip_long_lines で捨てられ得るので数えない: seq=-2）
sa count --term "request_id=$t" --term "event_name=log.record" --min "$n" --wait 300
sa collector-errors --term "request_id=$t" --min 1
step "fluent-bit: retries / drops / skipped long lines"
fb_metrics 'retr|drop|skip|long'
step "後続の行が詰まらずに届く（永久滞留しない）"
t2="$(tag bad-after)"
"$RUN_E2E" tool python deploy/logging/test/loggen.py --tag "$t2" --count 50 >/dev/null
sa count --term "request_id=$t2" --expect 50 --wait 300
