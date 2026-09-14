#!/usr/bin/env bash
# 本物の provider で production（画像・動画・音声 → マニフェスト）を1本流して検証する（ADR-0017）。
#
# **有料（fal）呼び出しを伴う。** 通常の pytest / CI からは走らない。所有者の明示操作のみ。
# 前提:
#   docker compose --profile core up -d --wait
#   API（POST /episodes/{id}/production を持つ版）
#   ./scripts/run-production-worker.sh / run-production-image-worker.sh /
#   run-production-voice-worker.sh / run-production-video-worker.sh（それぞれ別ターミナル）
#
# 使い方:
#   EPISODE_ID=<storyboard_ready|assets_ready の Episode> ./scripts/smoke-production.sh   # 見積もりだけ表示して止まる
#   EPISODE_ID=... CONFIRM_PAID=1 ./scripts/smoke-production.sh                          # 実行
#
# ガード: storyboard のシーン数 > MAX_SCENES（既定3）なら拒否。
# 見積もり: 画像 $IMAGE_USD/枚 + 動画 $VIDEO_USD_PER_SECOND/秒（storyboard の尺を秒へ切り上げ）。
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API:-http://localhost:8000}"
MAX_SCENES="${MAX_SCENES:-3}"
IMAGE_USD="${IMAGE_USD:-0.04}"
VIDEO_USD_PER_SECOND="${VIDEO_USD_PER_SECOND:-0.2419}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-3600}"
VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="../../.venv"

export DATABASE_URL="${DATABASE_URL:-postgresql+psycopg://avp:change-me@localhost:5432/avp}"
export MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
export MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
export MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}"
export MINIO_BUCKET="${MINIO_BUCKET:-artifacts}"

[ -n "${EPISODE_ID:-}" ] || { echo "NG: EPISODE_ID is required" >&2; exit 2; }

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

body=$(curl -sS -f "$API/episodes/$EPISODE_ID")
status=$(printf '%s' "$body" | json_field '["status"]')
case "$status" in
  storyboard_ready|assets_ready) ;;
  *) echo "NG: episode status is $status (need storyboard_ready or assets_ready)" >&2; exit 2 ;;
esac

# 現行 storyboard を MinIO から読み、sha を検証してシーン数と尺を数える
sb_key=$(printf '%s' "$body" | python3 -c '
import json,sys
arts=[a for a in json.load(sys.stdin)["artifacts"] if a["artifact_type"]=="storyboard"]
if not arts: sys.exit("NG: no storyboard artifact")
print(arts[-1]["object_key"], arts[-1]["sha256"])')
read -r SB_KEY SB_SHA <<<"$sb_key"

read -r scenes images video_seconds estimate <<<"$(
SB_KEY="$SB_KEY" SB_SHA="$SB_SHA" IMAGE_USD="$IMAGE_USD" VIDEO_USD="$VIDEO_USD_PER_SECOND" \
"$VENV/bin/python" - <<'PY'
import asyncio, math, os, sys
from contracts.artifacts import parse_storyboard_artifact
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.storage.minio_store import MinioArtifactStore

async def main() -> None:
    store = MinioArtifactStore.from_settings(Settings())
    payload = await store.get_json(os.environ["SB_KEY"])
    if sha256_hex(canonical_json_bytes(payload)) != os.environ["SB_SHA"]:
        sys.exit("NG: storyboard sha256 mismatch")
    sb = parse_storyboard_artifact(payload)
    n = len(sb.scenes)
    seconds = sum(math.ceil(s.duration_ms / 1000) for s in sb.scenes)
    cost = n * float(os.environ["IMAGE_USD"]) + seconds * float(os.environ["VIDEO_USD"])
    print(n, n, seconds, f"{cost:.2f}")

asyncio.run(main())
PY
)"

echo "episode      : $EPISODE_ID ($status)"
echo "scenes       : $scenes (MAX_SCENES=$MAX_SCENES)"
echo "estimate     : images $images x \$$IMAGE_USD + video ${video_seconds}s x \$$VIDEO_USD_PER_SECOND = \$$estimate (upper bound for first run; reuse is free)"

if [ "$scenes" -gt "$MAX_SCENES" ]; then
  echo "NG: storyboard has $scenes scenes > MAX_SCENES=$MAX_SCENES; refusing" >&2
  exit 3
fi
if [ "${CONFIRM_PAID:-}" != "1" ]; then
  echo "STOP: paid provider calls. Re-run with CONFIRM_PAID=1 to proceed." >&2
  exit 4
fi

echo "== POST /episodes/$EPISODE_ID/production =="
curl -sS -f -X POST "$API/episodes/$EPISODE_ID/production" | python3 -m json.tool

echo "== assets_ready を待つ =="
sleep 5
deadline=$((SECONDS + TIMEOUT_SECONDS))
while :; do
  body=$(curl -sS -f "$API/episodes/$EPISODE_ID")
  status=$(printf '%s' "$body" | json_field '["status"]')
  echo "status=$status"
  case "$status" in
    assets_ready)
      # admit 前の古い assets_ready を見ていないか: production job が全て終端か確認
      open=$(printf '%s' "$body" | python3 -c '
import json,sys
jobs=[j for j in json.load(sys.stdin)["jobs"] if j["type"].startswith("produce_scene_")]
print(sum(1 for j in jobs if j["status"] in ("queued","running")), len(jobs))')
      read -r n_open n_jobs <<<"$open"
      [ "$n_open" = 0 ] && [ "$n_jobs" -gt 0 ] && break ;;
    failed|blocked|cancelled|needs_work)
      printf '%s' "$body" | python3 -m json.tool >&2
      echo "NG: episode ended in $status" >&2
      exit 1 ;;
  esac
  [ "$SECONDS" -lt "$deadline" ] || { echo "NG: timed out (status=$status)" >&2; exit 1; }
  sleep 10
done

echo "== マニフェストとメディアを MinIO から読み戻して検証 =="
SMOKE_EPISODE_ID="$EPISODE_ID" "$VENV/bin/python" - <<'PY'
import asyncio, os, sys
from contracts.artifacts import (
    parse_production_manifest, parse_scene_image_artifact, parse_scene_video_artifact,
    parse_scene_voice_artifact, parse_script_artifact, parse_storyboard_artifact,
)
from contracts.states import ArtifactType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.production.manifest import check_manifest_coverage
from infrastructure.config import Settings
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.artifact_store import readback_sha256
from infrastructure.storage.minio_store import MinioArtifactStore

async def main() -> None:
    settings = Settings()
    store = MinioArtifactStore.from_settings(settings)
    ep = os.environ["SMOKE_EPISODE_ID"]
    async with session_factory_from_settings(settings)() as session:
        repo = ArtifactMetadataRepository(session)
        cur = {t: await repo.list_current_by_type(ep, t) for t in ArtifactType}

    async def verified(meta):
        payload = await store.get_json(meta.object_key)
        if sha256_hex(canonical_json_bytes(payload)) != meta.sha256:
            sys.exit(f"NG: sha256 mismatch for {meta.object_key}")
        return payload

    (manifest_meta,) = cur[ArtifactType.PRODUCTION_MANIFEST]
    (sb_meta,) = cur[ArtifactType.STORYBOARD]
    (script_meta,) = cur[ArtifactType.SCRIPT]
    manifest = parse_production_manifest(await verified(manifest_meta))
    check_manifest_coverage(
        manifest,
        parse_storyboard_artifact(await verified(sb_meta)),
        parse_script_artifact(await verified(script_meta)),
        storyboard_sha256=sb_meta.sha256,
        script_sha256=script_meta.sha256,
    )
    checked = 0
    for t, parse in ((ArtifactType.SCENE_IMAGE, parse_scene_image_artifact),
                     (ArtifactType.SCENE_VIDEO, parse_scene_video_artifact),
                     (ArtifactType.SCENE_VOICE, parse_scene_voice_artifact)):
        for meta in cur[t]:
            art = parse(await verified(meta))
            if await readback_sha256(store, art.media.object_key) != art.media.sha256:
                sys.exit(f"NG: media sha256 mismatch {art.media.object_key}")
            checked += 1
    print(f"OK: manifest {manifest_meta.sha256[:12]}... covers {len(manifest.scenes)} scenes / "
          f"{len(manifest.voices)} voices; {checked} media readbacks match")

asyncio.run(main())
PY
echo "OK: production smoke passed"
