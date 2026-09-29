#!/usr/bin/env bash
# deploy などを「Daily Schedule の maintenance pause」で包む（ADR-0027）。
#
#   scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m -- <command...>
#
# pause → コマンド → **コマンドが成功したときだけ** trap で end（unpause + describe 確認）。
# コマンドの終了コードをそのまま返す。ただし end が失敗したら、コマンドが成功でも非0で終わる。
#
# - begin が「運用者の pause（印なし）が有効」(exit 3) を返したら、コマンドは実行するが
#   **解除はしない**（運用者の停止を deploy が外さない）
# - begin が失敗（Schedule 不在・Temporal に届かない等）したらコマンドを実行しない
# - **コマンドが失敗（非0）・Ctrl-C・SIGTERM で終わったときは unpause しない**。
#   壊れた状態のまま自動生成を再開しない（このガードの唯一の目的）。maintenance pause は
#   印付きのまま残るので、TTL 切れで watchdog が解除するまで、または運用者が原因を直して
#   `schedule-guard.py reconcile` / `maintenance end` を手で呼ぶまで、自動生成は始まらない
# - TTL 切れによる解除は watchdog（`infrastructure/temporal/schedule_guard.py` の
#   `reconcile_schedule`）だけの役目。このスクリプトはTTL経過を「成功」の代わりに使わない
#
# SCHEDULE_GUARD でガード CLI を差し替えられる（テスト用）。
set -uo pipefail
cd "$(dirname "$0")/.."

GUARD="${SCHEDULE_GUARD:-python scripts/schedule-guard.py}"
reason="" ttl=""
while [ $# -gt 0 ]; do
  case "$1" in
    --reason) reason="$2"; shift 2 ;;
    --ttl) ttl="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "usage: $0 --reason R [--ttl 45m] -- command..." >&2; exit 2 ;;
  esac
done
[ -n "$reason" ] && [ $# -gt 0 ] || { echo "usage: $0 --reason R [--ttl 45m] -- command..." >&2; exit 2; }

ttl_args=()
[ -n "$ttl" ] && ttl_args=(--ttl "$ttl")

# shellcheck disable=SC2086
$GUARD maintenance begin --reason "$reason" "${ttl_args[@]}"
begin_rc=$?
release=1
case "$begin_rc" in
  0) ;;
  3) echo "NOTE: Daily Schedule is paused by an operator; running the command but NOT unpausing" >&2
     release=0 ;;
  *) echo "ERROR: could not start the maintenance pause (rc=$begin_rc); command not run" >&2
     exit "$begin_rc" ;;
esac

cmd_rc=0
finished=0
finish() {
  [ "$finished" = 1 ] && return
  finished=1
  local rc=$cmd_rc
  if [ "$release" = 1 ] && [ "$rc" = 0 ]; then
    # shellcheck disable=SC2086
    if ! $GUARD maintenance end; then
      echo "ERROR: maintenance end failed: the Daily Schedule may still be paused. Run: $GUARD reconcile" >&2
      rc=1
    fi
  elif [ "$release" = 1 ]; then
    echo "NOTE: command failed or was interrupted (rc=$rc); NOT unpausing the Daily Schedule." >&2
    echo "      Automation stays paused until you fix the problem and run one of:" >&2
    echo "        $GUARD status          # see current state" >&2
    echo "        $GUARD reconcile       # release only if the maintenance TTL has expired" >&2
    echo "        $GUARD maintenance end # release now, once you've confirmed it's safe" >&2
  fi
  exit "$rc"
}
trap finish EXIT
trap 'cmd_rc=130; exit 130' INT
trap 'cmd_rc=143; exit 143' TERM

"$@"
cmd_rc=$?
exit "$cmd_rc"
