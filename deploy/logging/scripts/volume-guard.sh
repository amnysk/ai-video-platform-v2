#!/usr/bin/env bash
# one-shot（OpenSearch のイメージ、Fluent Bit の前に毎回走る）: 状態 volume が正しく mount されているかを確かめる。
# - sentinel が無い = security-init 前、または project 名の違いで空の volume が新しく作られた
# - 位置 DB が無いのに read_from_head=true = 既存の大きなログを全量取り込もうとしている（導入手順の誤り）
#   ただし AVP_LOG_REREAD=yes（読み直しモード）を明示したときは通す: 位置 DB を消して残っている json-file を
#   先頭から読み直す（I-26 の欠損を埋める。runbook §7・platform.md §3。I-28）
# パスは試験のためだけに差し替えられる（AVP_GUARD_STATE_DIR / AVP_GUARD_CONTAINERS_DIR）
set -euo pipefail
STATE="${AVP_GUARD_STATE_DIR:-/fb-state}"
CONTAINERS="${AVP_GUARD_CONTAINERS_DIR:-/containers}"
FROM_HEAD="${AVP_LOG_READ_FROM_HEAD:-true}"
REREAD="${AVP_LOG_REREAD:-}"
S="$STATE/.avp-logging-sentinel"
if [[ ! -f "$S" ]]; then
  echo "volume-guard: $S が無い。security-init を先に流すこと（空の volume で起動しない）" >&2
  exit 1
fi
if [[ "$REREAD" == "yes" ]]; then
  if [[ -f "$STATE/tail.db" ]]; then
    echo "volume-guard: 読み直しモードだが位置 DB（tail.db）が残っている。先に消すこと（runbook §7）" >&2
    exit 1
  fi
  if [[ "$FROM_HEAD" != "true" ]]; then
    echo "volume-guard: 読み直しモードには AVP_LOG_READ_FROM_HEAD=true が要る（false だと末尾から読み、何も読み直さない）" >&2
    exit 1
  fi
  echo "volume-guard: 読み直しモード: 位置 DB が無いので残っている json-file を先頭から読み直す（infra は重複する）"
elif [[ ! -f "$STATE/tail.db" && "$FROM_HEAD" == "true" ]]; then
  echo "volume-guard: 位置 DB が無い。初回は AVP_LOG_READ_FROM_HEAD=false で起動して位置 DB を作ること。" \
    "消した位置 DB から読み直すときだけ AVP_LOG_REREAD=yes を付ける（runbook §7）" >&2
  exit 1
fi
if [[ ! -d "$CONTAINERS" ]] || ! ls "$CONTAINERS" >/dev/null 2>&1; then
  echo "volume-guard: $CONTAINERS が読めない（AVP_LOG_CONTAINERS_DIR を確認）" >&2
  exit 1
fi
echo "volume-guard: ok"
