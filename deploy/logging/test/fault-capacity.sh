#!/usr/bin/env bash
# S-CAP: buffer 上限（storage.total_limit_size）を超えたら古い chunk から破棄され、業務側は止まらない。
# **ホストのディスクを使わない**: B の compose で Fluent Bit の storage を容量制限した tmpfs
# （例 FLUENTBIT_STORAGE_TMPFS_SIZE=64m、storage.total_limit_size=16M）に差し替えて起動しておくこと。
#   usage: fault-capacity.sh [行数=200000] [pad=400] [rate 行/秒=2000]
# rate を絞るのは、json-file の rotation（既定 20m × 5）が Collector の読み取りより先に一巡して
# 「buffer の破棄」ではなく「rotation の欠損」を測ってしまわないため（2026-10-06 実測: 全速の
# 200000 行は数秒で 100MB を超え、118114 行が rotation で読まれる前に消えた。buffer は 22MB で上限未達）。
source "$(dirname "$0")/lib.sh"
n="${1:-200000}"; pad="${2:-400}"; rate="${3:-2000}"; t="$(tag cap)"
os="$(container_of "$OPENSEARCH_SERVICE")"; fb="$(container_of "$FLUENTBIT_SERVICE")"
step "storage mount of $fb（tmpfs であることを確認。違えば中止）"
docker inspect "$fb" --format '{{json .HostConfig.Tmpfs}} {{json .Mounts}}'
docker inspect "$fb" --format '{{json .HostConfig.Tmpfs}}' | grep -q . || { echo "NG: tmpfs でない" >&2; exit 2; }
df -h / | tail -1
step "before"; fb_metrics 'output_(dropped|retries_failed)'
step "stop $os"; docker stop "$os" >/dev/null
start=$(date +%s)
"$RUN_E2E" tool python deploy/logging/test/loggen.py --tag "$t" --count "$n" --pad "$pad" --rate "$rate" >/dev/null
echo "loggen elapsed: $(( $(date +%s) - start ))s（アプリ側が止まらないこと）"
step "Collector が json-file を読み切るまで待つ（最大 300 秒）"
for _ in $(seq 60); do
  c="$(catchup || true)"; echo "$c"
  echo "$c" | grep -q '"behind_bytes": 0' && break
  sleep 5
done
step "buffer usage / drops while down"; fb_metrics 'output_(dropped|retries_failed)|storage'
fb_fs 'du -sh /proc/1/root/fb-buffer' || true; fb_storage | head -c 400; echo
df -h / | tail -1
step "start $os"; docker start "$os" >/dev/null
for _ in $(seq 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$os")" = healthy ] && break
  sleep 5
done
# 期待: 全件ではない（破棄が起きた）が、新しい側は届く。dropped_records_total が増えている
sa count --term "request_id=$t" --min 1 --wait 600
step "件数が 30 秒変わらなくなるまで待つ（最大 600 秒）"
prev=-1
for _ in $(seq 20); do
  cur="$(sa count --term "request_id=$t" | sed -E 's/.*"count": ([0-9]+).*/\1/')"
  echo "count=$cur"; [ "$cur" = "$prev" ] && break; prev="$cur"; sleep 30
done
sa count --term "request_id=$t" --min 1 --max "$(( n - 1 ))"
sa dupes --term "request_id=$t" --expect 0
step "after"; fb_metrics 'output_(dropped|retries_failed)'
