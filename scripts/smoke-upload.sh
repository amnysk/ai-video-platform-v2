#!/usr/bin/env bash
# upload 工程（ADR-0020）を本物の Temporal / PostgreSQL / MinIO と **fake の YouTube** で流す。
# 実 YouTube には一切接続しない（tests/support/fake_upload_worker.py、AVP_FAKE_YOUTUBE=1 必須）。
#
#   EPISODE_ID=<render_ready の Episode> ./scripts/smoke-upload.sh
#   ./scripts/smoke-upload.sh     # EPISODE_ID が無ければ smoke-render.sh で合成 Episode を作る
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="${AVP_VENV:-../../.venv}"
[ -x "$VENV/bin/python" ] || { echo "NG: python venv not found" >&2; exit 1; }
VENV="$(cd "$VENV" && pwd)"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-300}"
LOG_DIR="${LOG_DIR:-$(mktemp -d -t smoke-upload.XXXXXX)}"
mkdir -p "$LOG_DIR"

if [ -z "${DATABASE_URL:-}" ]; then
  pg_pw="$(docker exec avp2-postgres-1 printenv POSTGRES_PASSWORD)"
  export DATABASE_URL="postgresql+psycopg://avp:${pg_pw}@localhost:5432/avp"
fi
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
[ -n "${MINIO_ACCESS_KEY:-}" ] || MINIO_ACCESS_KEY="$(docker exec avp2-minio-1 printenv MINIO_ROOT_USER)"
[ -n "${MINIO_SECRET_KEY:-}" ] || MINIO_SECRET_KEY="$(docker exec avp2-minio-1 printenv MINIO_ROOT_PASSWORD)"
export MINIO_ACCESS_KEY MINIO_SECRET_KEY
export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"
export TEMPORAL_ADDRESS="${TEMPORAL_ADDRESS:-localhost:7233}"
export AI_VIDEO_WORK_ROOT="${AI_VIDEO_WORK_ROOT:-/mnt/minio-hdd/ai-video-work}"

PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

py() { PYTHONPATH=. "$VENV/bin/python" scripts/smoke_upload.py "$@"; }
json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

if [ -z "${EPISODE_ID:-}" ]; then
  echo "== no EPISODE_ID; creating a render_ready episode via smoke-render.sh =="
  ./scripts/smoke-render.sh >"$LOG_DIR/render.log" 2>&1 || { tail -30 "$LOG_DIR/render.log" >&2; exit 1; }
  EPISODE_ID="$(awk '/^episode +:/ {print $3}' "$LOG_DIR/render.log" | tail -n 1)"
fi
py check-input "$EPISODE_ID"
echo "episode : $EPISODE_ID"

if pgrep -f 'workers.upload.run_worker|fake_upload_worker' >/dev/null; then
  echo "NG: an upload worker is already polling the upload queues; refusing to race it" >&2
  exit 2
fi

port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
"$VENV/bin/uvicorn" apps.api.main:app --host 127.0.0.1 --port "$port" >"$LOG_DIR/api.log" 2>&1 &
PIDS+=($!)
API="http://127.0.0.1:$port"
for _ in $(seq 1 60); do curl -sS -f "$API/openapi.json" >/dev/null 2>&1 && break; sleep 0.5; done
curl -sS -f "$API/openapi.json" | grep -q '/episodes/{episode_id}/upload' \
  || { echo "NG: local API has no upload route" >&2; tail -20 "$LOG_DIR/api.log" >&2; exit 1; }

echo "== starting FAKE upload worker (log $LOG_DIR/worker.log) =="
AVP_FAKE_YOUTUBE=1 PYTHONPATH=. "$VENV/bin/python" -m tests.support.fake_upload_worker \
  >"$LOG_DIR/worker.log" 2>&1 &
PIDS+=($!)
sleep 4
kill -0 "${PIDS[-1]}" 2>/dev/null || { echo "NG: worker exited" >&2; tail -20 "$LOG_DIR/worker.log" >&2; exit 1; }

code=$(curl -sS -o "$LOG_DIR/post.json" -w '%{http_code}' -X POST "$API/episodes/$EPISODE_ID/upload")
[ "$code" = 202 ] || { echo "NG: POST upload -> $code $(cat "$LOG_DIR/post.json")" >&2; exit 1; }
echo "POST upload : $code"

deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
  status=$(curl -sS -f "$API/episodes/$EPISODE_ID" | json_field '["status"]')
  case "$status" in
    uploaded) break ;;
    blocked|needs_work|failed|cancelled)
      echo "NG: episode ended in $status (worker log $LOG_DIR/worker.log)" >&2; exit 1 ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || { echo "NG: timed out (status=$status)" >&2; exit 1; }
  sleep 2
done
echo "status : uploaded"

result=$(py verify "$EPISODE_ID")
echo "verify : $result"

code=$(curl -sS -o "$LOG_DIR/post2.json" -w '%{http_code}' -X POST "$API/episodes/$EPISODE_ID/upload")
[ "$code" = 409 ] || { echo "NG: re-POST upload -> $code (want 409)" >&2; exit 1; }
echo "re-POST : 409 $(cat "$LOG_DIR/post2.json")"
sleep 3
py verify "$EPISODE_ID" >/dev/null

counters=$(grep 'FAKE_YOUTUBE final_chunk' "$LOG_DIR/worker.log" | tail -n 1)
echo "fake    : ${counters#*FAKE_YOUTUBE }"
grep -q 'videos_created=1 ' <<<"$counters" || { echo "NG: fake videos_created != 1" >&2; exit 1; }
if grep -q 'fake-youtube://session' "$LOG_DIR/worker.log" "$LOG_DIR/api.log"; then
  echo "NG: session URI leaked into logs" >&2; exit 1
fi
echo "OK: upload smoke passed (fake YouTube, no network upload)"
