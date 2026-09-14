#!/usr/bin/env bash
# Render worker を起動する（ADR-0019）。RenderWorkflow・状態系 Activity・描画 Activity（queue render）。
#
#   docker compose --profile core up -d --wait
#   ./scripts/install-render-ffmpeg.sh     # 初回のみ。表示される RENDER_FFMPEG_* を設定する
#   ./scripts/run-render-worker.sh
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

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://avp:change-me@localhost:5432/avp}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}"
export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"
export AI_VIDEO_WORK_ROOT="${AI_VIDEO_WORK_ROOT:-/mnt/minio-hdd/ai-video-work}"
export RENDER_FFMPEG_PATH="${RENDER_FFMPEG_PATH:-$HOME/.local/share/avp/ffmpeg/7.1.1/ffmpeg}"
if [ -z "${RENDER_FFMPEG_SHA256:-}" ]; then
  echo "NG: RENDER_FFMPEG_SHA256 が未設定です（scripts/install-render-ffmpeg.sh の出力を設定）" >&2
  exit 1
fi
export RENDER_FFMPEG_SHA256

echo "render worker starting"
echo "  temporal    : $TEMPORAL_ADDRESS"
echo "  database    : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio       : $MINIO_ENDPOINT"
echo "  work root   : $AI_VIDEO_WORK_ROOT"
echo "  ffmpeg      : $RENDER_FFMPEG_PATH"
exec "$VENV/bin/python" -m workers.render.run_worker
