#!/usr/bin/env bash
# Upload worker を起動する（ADR-0020）。UploadWorkflow・状態系 Activity（queue upload）と
# 投稿 Activity（queue upload-media、並行数 1）。YouTube には private でしか投稿しない。
#
#   docker compose --profile core up -d --wait
#   python scripts/youtube-oauth.py   # 初回のみ。refresh token を repo 外の 0600 ファイルへ
#   ./scripts/run-upload-worker.sh
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

for name in YOUTUBE_CLIENT_ID YOUTUBE_CLIENT_SECRET YOUTUBE_REFRESH_TOKEN_PATH YOUTUBE_CHANNEL_ID; do
  if [ -z "${!name:-}" ]; then
    echo "NG: $name が未設定です（docs/operations/upload-worker.md）" >&2
    exit 1
  fi
done

echo "upload worker starting"
echo "  temporal    : $TEMPORAL_ADDRESS"
echo "  database    : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio       : $MINIO_ENDPOINT"
echo "  work root   : $AI_VIDEO_WORK_ROOT"
echo "  channel     : $YOUTUBE_CHANNEL_ID"
echo "  paused      : ${UPLOADS_PAUSED:-false}"
exec "$VENV/bin/python" -m workers.upload.run_worker
