#!/usr/bin/env bash
# Production Voice Worker を **Ubuntu ホストのプロセス**として起動する（ADR-0017 / Phase 4B）。
#
#   ./scripts/setup-piper.sh                     # 初回だけ（隔離 venv と音声モデル）
#   docker compose --profile core up -d --wait   # 基盤
#   ./scripts/run-production-voice-worker.sh     # 別ターミナルで worker
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

PIPER_HOME="${PIPER_HOME:-$HOME/.local/share/avp2/piper}"
export PIPER_PYTHON="${PIPER_PYTHON:-$PIPER_HOME/venv/bin/python}"
export PIPER_VOICE_PATH="${PIPER_VOICE_PATH:-$PIPER_HOME/voices/en_US-kristin-medium.onnx}"
if [ ! -x "$PIPER_PYTHON" ] || [ ! -f "$PIPER_VOICE_PATH" ]; then
  echo "NG: Piper が未セットアップです。./scripts/setup-piper.sh を実行してください" >&2
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

echo "production voice worker starting (host process)"
echo "  temporal : $TEMPORAL_ADDRESS"
echo "  database : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio    : $MINIO_ENDPOINT"
echo "  piper    : $PIPER_PYTHON"
echo "  voice    : $PIPER_VOICE_PATH"
exec "$VENV/bin/python" -m workers.production_voice.run_worker
