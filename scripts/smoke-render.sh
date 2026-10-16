#!/usr/bin/env bash
# render 工程（ADR-0019）を本物の ffmpeg / Temporal / PostgreSQL / MinIO で流して検証する。
# **有料呼び出しは無い**（素材は固定版 ffmpeg で合成する）。
#
# 前提: docker compose --profile core up -d --wait / ./scripts/install-render-ffmpeg.sh / alembic head
#
# 使い方:
#   ./scripts/smoke-render.sh                               # 合成 Episode を作って shorts_vertical → 再実行 skip → long_form_horizontal
#   EPISODE_ID=<assets_ready の Episode> ./scripts/smoke-render.sh   # Phase 4 の実出力を使う
#   RENDER_PROFILE_ID=long_form_horizontal ./scripts/smoke-render.sh # 先に描く profile を変える
#
# API: 既存の API（$API、既定 localhost:8000）が POST /episodes/{id}/render を持たなければ、
#      この worktree のコードで uvicorn を空きポートに起動する（共有コンテナには触らない）。
# worker: render worker が居なければこの worktree から起動し、終了時に止める（USE_RUNNING_WORKER=1 で既存を使う）。
set -euo pipefail
cd "$(dirname "$0")/.."

VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="${AVP_VENV:-../../.venv}"
[ -x "$VENV/bin/python" ] || { echo "NG: python venv not found" >&2; exit 1; }
VENV="$(cd "$VENV" && pwd)"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-900}"
FIRST_PROFILE="${RENDER_PROFILE_ID:-shorts_vertical}"
case "$FIRST_PROFILE" in
  shorts_vertical) SECOND_PROFILE=long_form_horizontal ;;
  long_form_horizontal) SECOND_PROFILE=shorts_vertical ;;
  *) echo "NG: unknown RENDER_PROFILE_ID=$FIRST_PROFILE" >&2; exit 2 ;;
esac
LOG_DIR="${LOG_DIR:-$(mktemp -d -t smoke-render.XXXXXX)}"

# --- 資格情報はコンテナから取る（表示しない） ---
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
export RENDER_FFMPEG_PATH="${RENDER_FFMPEG_PATH:-$HOME/.local/share/avp/ffmpeg/7.1.1/ffmpeg}"
export RENDER_FFMPEG_SHA256="${RENDER_FFMPEG_SHA256:-810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c}"
[ "$(sha256sum "$RENDER_FFMPEG_PATH" | cut -d' ' -f1)" = "$RENDER_FFMPEG_SHA256" ] \
  || { echo "NG: ffmpeg sha256 mismatch (run scripts/install-render-ffmpeg.sh)" >&2; exit 1; }

PIDS=()
cleanup() {
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

py() { PYTHONPATH=. "$VENV/bin/python" scripts/smoke_render.py "$@"; }
json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

# --- API ---
API="${API:-http://localhost:8000}"
if ! curl -sS -f "$API/openapi.json" 2>/dev/null | grep -q '/episodes/{episode_id}/render'; then
  port="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
  echo "== $API has no render route; starting local API on :$port (log $LOG_DIR/api.log) =="
  "$VENV/bin/uvicorn" apps.api.main:app --host 127.0.0.1 --port "$port" >"$LOG_DIR/api.log" 2>&1 &
  PIDS+=($!)
  API="http://127.0.0.1:$port"
  for _ in $(seq 1 60); do curl -sS -f "$API/openapi.json" >/dev/null 2>&1 && break; sleep 0.5; done
  curl -sS -f "$API/openapi.json" | grep -q '/episodes/{episode_id}/render' \
    || { echo "NG: local API did not come up" >&2; tail -20 "$LOG_DIR/api.log" >&2; exit 1; }
fi

# --- render worker ---
# compose の render-worker 等、host の pgrep から見えない poller も Temporal に問い合わせる
# （identity を問わない）。居れば、この worktree のコードで検証できないので拒否する
if [ "${USE_RUNNING_WORKER:-}" != "1" ]; then
  guard=0
  PYTHONPATH=. "$VENV/bin/python" -m infrastructure.temporal.poller_check \
    --queue render --queue render-media --identity-suffix '' >/dev/null || guard=$?
  if [ "$guard" -ne 1 ]; then
    echo "NG: the render queues already have a poller (or Temporal is unreachable: rc=$guard);" \
         "stop it first (e.g. docker compose stop render-worker; 停止直後は約2分待つ) or set USE_RUNNING_WORKER=1" >&2
    exit 2
  fi
fi
if pgrep -f 'workers.render.run_worker' >/dev/null; then
  if [ "${USE_RUNNING_WORKER:-}" != "1" ]; then
    echo "NG: a render worker is already running (pid $(pgrep -f workers.render.run_worker | tr '\n' ' '));" \
         "set USE_RUNNING_WORKER=1 to use it" >&2
    exit 2
  fi
else
  echo "== starting render worker (log $LOG_DIR/worker.log) =="
  VENV="$VENV" ./scripts/run-render-worker.sh >"$LOG_DIR/worker.log" 2>&1 &
  PIDS+=($!)
  sleep 5
  kill -0 "${PIDS[-1]}" 2>/dev/null || { echo "NG: render worker exited" >&2; tail -20 "$LOG_DIR/worker.log" >&2; exit 1; }
fi

# --- Episode ---
if [ -n "${EPISODE_ID:-}" ]; then
  py check-input "$EPISODE_ID"
  echo "== using existing episode $EPISODE_ID =="
else
  echo "== seeding synthetic assets_ready episode =="
  EPISODE_ID="$(py seed | tail -n 1)"
  echo "episode      : $EPISODE_ID"
fi

render_jobs() {
  curl -sS -f "$API/episodes/$EPISODE_ID" | python3 -c '
import json,sys
jobs=[j for j in json.load(sys.stdin)["jobs"] if j["type"]=="render_final_video"]
print(len(jobs), sum(1 for j in jobs if j["status"] in ("queued","running")))'
}

# $1=profile。POST して、新しい render job が終端し Episode が落ち着くまで待つ
render_and_wait() {
  local profile="$1" before n_open n_jobs status body
  read -r before _ <<<"$(render_jobs)"
  for attempt in $(seq 1 30); do
    code=$(curl -sS -o "$LOG_DIR/post.json" -w '%{http_code}' -X POST "$API/episodes/$EPISODE_ID/render" \
      -H 'content-type: application/json' -d "{\"render_profile_id\": \"$profile\"}")
    [ "$code" = 202 ] || [ "$code" = 200 ] && break
    if [ "$code" = 409 ] && [ "$attempt" -lt 30 ]; then sleep 2; continue; fi  # 前の workflow の終了待ち
    echo "NG: POST render ($profile) -> $code $(cat "$LOG_DIR/post.json")" >&2
    return 1
  done
  local deadline=$((SECONDS + TIMEOUT_SECONDS))
  while :; do
    body=$(curl -sS -f "$API/episodes/$EPISODE_ID")
    status=$(printf '%s' "$body" | json_field '["status"]')
    read -r n_jobs n_open <<<"$(render_jobs)"
    case "$status" in
      render_ready)
        [ "$n_jobs" -gt "$before" ] && [ "$n_open" = 0 ] && break ;;
      blocked|needs_work|failed|cancelled)
        [ "$n_open" = 0 ] && {
          printf '%s' "$body" | python3 -m json.tool >&2
          echo "NG: episode ended in $status (worker log: $LOG_DIR/worker.log)" >&2
          return 1
        } ;;
    esac
    [ "$SECONDS" -lt "$deadline" ] || { echo "NG: timed out (status=$status)" >&2; return 1; }
    sleep 3
  done
}

summary() {  # $1=label $2=json
  printf '%s' "$2" | LABEL="$1" python3 -c '
import json, os, sys
r = json.load(sys.stdin)
label, sha = os.environ["LABEL"], r["media_sha256"][:12]
print("%-11s: %s v%s job=%s" % (label, r["artifact_id"], r["version"], r["job"]))
print("             %s %sms (plan %sms, audio %sms) %sB sha %s" % (
    r["measured"], r["duration_ms"], r["plan_total_ms"], r["audio_duration_ms"], r["bytes"], sha))
print("             timeline %s cues=%s qa_checks=%s prev_superseded_at=%s" % (
    " ".join(r["timeline"]), r["subtitle_cues"], r["qa_checks"], r["previous_superseded_at"]))'
}
artifact_of() { printf '%s' "$1" | json_field '["artifact_id"]'; }

echo "== render $FIRST_PROFILE =="
render_and_wait "$FIRST_PROFILE"
first=$(py verify "$EPISODE_ID" --profile "$FIRST_PROFILE" --expect rendered)
first_id=$(artifact_of "$first")

echo "== render $FIRST_PROFILE again (must skip) =="
render_and_wait "$FIRST_PROFILE"
again=$(py verify "$EPISODE_ID" --profile "$FIRST_PROFILE" --expect skipped --previous "$first_id")

echo "== render $SECOND_PROFILE (new version) =="
render_and_wait "$SECOND_PROFILE"
second=$(py verify "$EPISODE_ID" --profile "$SECOND_PROFILE" --expect rendered --previous "$first_id")

echo
echo "episode    : $EPISODE_ID"
summary "first" "$first"
summary "skip" "$again"
summary "second" "$second"
echo "OK: render smoke passed"
