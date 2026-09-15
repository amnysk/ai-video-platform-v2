#!/usr/bin/env bash
# Pipeline worker を起動する（ADR-0023）。DailyEpisodeWorkflow / EpisodePipelineWorkflow と
# 状態系 Activity（queue pipeline）。工程の worker（script / storyboard / production* / render / upload）
# は別プロセスで起動しておくこと。Schedule の登録は scripts/ensure-daily-schedule.py。
#
#   docker compose --profile core up -d --wait
#   ./scripts/run-pipeline-worker.sh
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
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
export TEMPORAL_NAMESPACE="${TEMPORAL_NAMESPACE:-default}"

echo "pipeline worker starting"
echo "  temporal       : $TEMPORAL_ADDRESS"
echo "  database       : ${DATABASE_URL%%://*}://<redacted>"
echo "  paused         : ${PAUSED:-false}"
echo "  uploads paused : ${UPLOADS_PAUSED:-false}"
exec "$VENV/bin/python" -m workers.pipeline.run_worker
