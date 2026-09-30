#!/usr/bin/env bash
# S-RES: 資源測定。ログ基盤と隔離 app スタックの docker stats と MemAvailable / swap を CSV に追記。
#   usage: measure.sh [出力 CSV=$XDG_RUNTIME_DIR/avp2-oslog-measure.csv] [回数=1] [間隔秒=10]
source "$(dirname "$0")/lib.sh"
out="${1:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-measure.csv}"; times="${2:-1}"; every="${3:-10}"
[ -s "$out" ] || echo "ts,name,cpu,mem_usage,mem_pct,mem_available_kib,swap_free_kib" >"$out"
for _ in $(seq "$times"); do
  ts="$(date -u +%FT%TZ)"
  avail="$(awk '/MemAvailable/{print $2}' /proc/meminfo)"; swapf="$(awk '/SwapFree/{print $2}' /proc/meminfo)"
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}' \
    $(docker ps -q --filter "label=com.docker.compose.project=$LOGGING_PROJECT") \
    $(docker ps -q --filter "label=com.docker.compose.project=avp2-oslog-c") 2>/dev/null \
    | sed "s#^#$ts,#; s#\$#,$avail,$swapf#" >>"$out"
  sleep "$every"
done
tail -n 20 "$out"
