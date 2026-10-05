#!/usr/bin/env bash
# S-CAP: buffer 上限（storage.total_limit_size）を超えたら古い chunk から破棄され、業務側は止まらない。
# **ホストのディスクを使わない**: B の compose で Fluent Bit の storage を容量制限した tmpfs
# （例 FLUENTBIT_STORAGE_TMPFS_SIZE=64m、storage.total_limit_size=16M）に差し替えて起動しておくこと。
#   usage: fault-capacity.sh [行数=200000] [pad=400]
source "$(dirname "$0")/lib.sh"
n="${1:-200000}"; pad="${2:-400}"; t="$(tag cap)"
os="$(container_of "$OPENSEARCH_SERVICE")"; fb="$(container_of "$FLUENTBIT_SERVICE")"
step "storage mount of $fb（tmpfs であることを確認。違えば中止）"
docker inspect "$fb" --format '{{json .HostConfig.Tmpfs}} {{json .Mounts}}'
docker inspect "$fb" --format '{{json .HostConfig.Tmpfs}}' | grep -q . || { echo "NG: tmpfs でない" >&2; exit 2; }
df -h / | tail -1
step "stop $os"; docker stop "$os" >/dev/null
"$RUN_E2E" tool python deploy/logging/test/loggen.py --tag "$t" --count "$n" --pad "$pad" >/dev/null
sleep 30
step "buffer usage / drops while down"; fb_metrics 'storage|drop|chunk'
fb_fs 'du -sh /proc/1/root/fb-buffer' || true; fb_storage | head -c 400; echo
df -h / | tail -1
step "start $os"; docker start "$os" >/dev/null
# 期待: 全件ではない（破棄が起きた）が、新しい側は届く。dropped_records_total が増えている
sa count --term "request_id=$t" --min 1 --max "$(( n - 1 ))" --wait 600
fb_metrics 'drop'
