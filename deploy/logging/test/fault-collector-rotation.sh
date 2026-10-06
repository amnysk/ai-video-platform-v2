#!/usr/bin/env bash
# S-FAULT-ROT: Fluent Bit 停止中に rotation（一巡しない範囲）が起きても、再開後に全件・重複 0。
# 隔離 app スタックを小さい rotation（APPTEST_LOG_MAX_SIZE=1m, MAX_FILE=5）で動かす前提。
#   usage: fault-collector-rotation.sh [行数=20000] [pad=200]
# 一巡させる版（検知できない欠損の実演）: 行数を max-size*max-file/行長 より大きくし、
#   期待値を --expect ではなく --max で見る（ADR-0040 §5 の「受け入れる欠損」）。
source "$(dirname "$0")/lib.sh"
n="${1:-20000}"; pad="${2:-200}"; t="$(tag rot)"
fb="$(container_of "$FLUENTBIT_SERVICE")"; [ -n "$fb" ] || { echo "Fluent Bit コンテナが無い" >&2; exit 2; }
name="$AVP_LOG_TARGET_PROJECT-tool-$t"
step "stop $fb"; docker stop "$fb" >/dev/null
step "emit $n lines (tag=$t pad=$pad)"
"$RUN_E2E" tool --name "$name" python deploy/logging/test/loggen.py --tag "$t" --count "$n" --pad "$pad" >/dev/null
logpath="$(docker inspect "$name" --format '{{.LogPath}}')"
ls -la "$(dirname "$logpath")" | grep json.log || true
step "start $fb"; docker start "$fb" >/dev/null
sa count --term "request_id=$t" --expect "$n" --wait 600
sa dupes --term "request_id=$t" --expect 0
echo "container $name is kept for inspection (run-e2e.sh down で消える)"
