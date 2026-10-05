#!/usr/bin/env bash
# one-shot（OpenSearch のイメージ、Fluent Bit の前に毎回走る）: 状態 volume が正しく mount されているかを確かめる。
# - sentinel が無い = security-init 前、または project 名の違いで空の volume が新しく作られた
# - 位置 DB が無いのに read_from_head=true = 既存の大きなログを全量取り込もうとしている（導入手順の誤り）
set -euo pipefail
S=/fb-state/.avp-logging-sentinel
if [[ ! -f "$S" ]]; then
  echo "volume-guard: $S が無い。security-init を先に流すこと（空の volume で起動しない）" >&2
  exit 1
fi
if [[ ! -f /fb-state/tail.db && "${AVP_LOG_READ_FROM_HEAD:-true}" == "true" ]]; then
  echo "volume-guard: 位置 DB が無い。初回は AVP_LOG_READ_FROM_HEAD=false で起動して位置 DB を作ること" >&2
  exit 1
fi
if [[ ! -d /containers ]] || ! ls /containers >/dev/null 2>&1; then
  echo "volume-guard: /containers が読めない（AVP_LOG_CONTAINERS_DIR を確認）" >&2
  exit 1
fi
echo "volume-guard: ok"
