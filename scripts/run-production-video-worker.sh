#!/usr/bin/env bash
# Production Video Worker を **ホストプロセス**として起動する（ADR-0017 Phase 4C）。
#
# 有料 provider（fal）を呼ぶ。FAL_KEY は .env かシェル環境に置く（コミットしない）。
#
#   docker compose --profile core up -d --wait        # 基盤
#   ./scripts/run-production-video-worker.sh          # 別ターミナルで worker
set -euo pipefail

cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
if [ ! -x "$VENV/bin/python" ]; then
  VENV="${AVP_VENV:-../../.venv}"
fi
if [ ! -x "$VENV/bin/python" ]; then
  echo "NG: python venv が見つかりません（VENV=$VENV）" >&2
  exit 1
fi

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://avp:change-me@localhost:5432/avp}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}"
export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
export AI_VIDEO_WORK_ROOT="${AI_VIDEO_WORK_ROOT:-/mnt/minio-hdd/ai-video-work}"

if [ -z "${FAL_KEY:-}" ] && ! grep -qE '^FAL_KEY=.+' .env 2>/dev/null; then
  echo "NG: FAL_KEY が未設定です（有料 provider を呼ぶ worker）" >&2
  exit 1
fi

echo "production video worker starting (host process)"
echo "  temporal  : $TEMPORAL_ADDRESS"
echo "  database  : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio     : $MINIO_ENDPOINT"
echo "  work root : $AI_VIDEO_WORK_ROOT"
echo "  fal key   : <set>"
exec "$VENV/bin/python" -m workers.production_video.run_worker
