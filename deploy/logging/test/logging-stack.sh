#!/usr/bin/env bash
# 隔離試験用のログ基盤（B の compose.logging.yaml + compose.test.yaml）を、project LOGGING_PROJECT
# （既定 avp2-oslog-c-log）で起動・停止する。手順は docs/observability/platform.md §6 / §9 と同じ。
#   logging-stack.sh up        秘密の生成 → security-init → opensearch → bootstrap → fluent-bit（二段）
#   logging-stack.sh bootstrap bootstrap だけ（冪等の確認用）
#   logging-stack.sh stop      全コンテナを止める（volume は残す）
#   logging-stack.sh down      コンテナ・network・volume を消す（この project だけ）
#   logging-stack.sh ps
# APPTEST_ISM=prod: 短縮 ISM（compose.test.yaml の ism-test。2件/2分で rollover・3分後に削除）ではなく
#   本番の policy で bootstrap する（e2e の証拠が数分で消えないように。S-IDX 以外の試験はこちら）
source "$(dirname "$0")/lib.sh"
case "${1:-ps}" in
  up)
    "$SRC/deploy/logging/scripts/init-secrets.sh" --env test --dir "$AVP_LOGGING_SECRETS_DIR"
    ldc run --rm security-init
    ldc up -d --wait opensearch
    if [ "${APPTEST_ISM:-test}" = prod ]; then
      ldc run --rm -e AVP_ISM_POLICY_DIR=ism bootstrap
    else
      ldc run --rm bootstrap
    fi
    if ! docker volume inspect "$FLUENTBIT_STATE_VOLUME" >/dev/null 2>&1 \
       || [ -z "$(docker ps -aq --filter "label=com.docker.compose.project=$LOGGING_PROJECT" --filter label=com.docker.compose.service=fluent-bit)" ]; then
      # 導入時と同じ二段構え: 位置 DB を作ってから read_from_head=true（本番の既存ログを全量読まない）
      AVP_LOG_READ_FROM_HEAD=false ldc up -d fluent-bit
      sleep 15
    fi
    ldc up -d fluent-bit
    ;;
  bootstrap) ldc run --rm bootstrap ;;
  stop) ldc --profile dashboards stop ;;
  down) ldc --profile dashboards --profile setup --profile dashboards-setup down -v ;;
  ps) ldc ps -a ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
