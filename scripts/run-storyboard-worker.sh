#!/usr/bin/env bash
# Storyboard Worker を **Ubuntu ホストのプロセス**として起動する（ADR-0015 / ADR-0016）。
#
# Codex CLI と OpenMontage checkout はホストにある。Temporal / PostgreSQL / MinIO は compose 側。
#
#   docker compose --profile core up -d --wait   # 基盤
#   ./scripts/run-storyboard-worker.sh           # 別ターミナルで worker
#
# OpenMontage checkout は**読み取り専用**で使う（固定 commit の blob だけを git show で読む）。
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
export AI_VIDEO_WORK_ROOT="${AI_VIDEO_WORK_ROOT:-/mnt/minio-hdd/ai-video-work}"
if [ -z "${OPENMONTAGE_REPO_PATH:-}" ]; then
  # 本体 checkout 直下の ai-toolbox（<repo>/ai-toolbox/repos/OpenMontage）。worktree からでも同じ場所を指す。
  common_dir="$(git rev-parse --path-format=absolute --git-common-dir)"
  OPENMONTAGE_REPO_PATH="$(cd "$common_dir/.." && pwd)/ai-toolbox/repos/OpenMontage"
fi
export OPENMONTAGE_REPO_PATH
if ! git -C "$OPENMONTAGE_REPO_PATH" rev-parse --git-dir >/dev/null 2>&1; then
  echo "NG: OPENMONTAGE_REPO_PATH が git checkout ではありません: $OPENMONTAGE_REPO_PATH" >&2
  exit 1
fi

echo "storyboard worker starting (host process)"
echo "  temporal    : $TEMPORAL_ADDRESS"
echo "  database    : ${DATABASE_URL%%://*}://<redacted>"
echo "  minio       : $MINIO_ENDPOINT"
echo "  work root   : $AI_VIDEO_WORK_ROOT"
echo "  openmontage : $OPENMONTAGE_REPO_PATH"
exec "$VENV/bin/python" -m workers.storyboard.run_worker
