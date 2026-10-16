#!/usr/bin/env bash
# Production Workflow worker を起動する（ADR-0017）。状態系 Activity と ProductionWorkflow だけ。
#
# 画像・音声・動画の Activity は別 worker（production-image / production-voice / production-video）。
#
#   docker compose --profile core up -d --wait
#   ./scripts/run-production-worker.sh
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
if [ ! -x "$VENV/bin/python" ]; then
  # worktree で作業している場合は本体の venv を使う
  VENV="${AVP_VENV:-../../.venv}"
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "NG: python venv が見つかりません（VENV=$VENV）" >&2
  exit 1
fi

# ホストから見た接続先。compose のポート公開に合わせる。
export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://avp:change-me@localhost:5432/avp}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}"
export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"

echo "production workflow worker starting"
echo "  temporal    : $TEMPORAL_ADDRESS"
echo "  database    : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio       : $MINIO_ENDPOINT"
exec "$VENV/bin/python" -m workers.production.run_worker
