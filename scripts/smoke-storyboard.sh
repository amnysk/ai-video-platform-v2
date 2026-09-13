#!/usr/bin/env bash
# 本物の Codex + OpenMontage 仕様で storyboard を1本生成し、MinIO から読み戻して検証する。
#
# **外部AI呼び出し（課金）を伴う。** 通常の pytest / CI からは走らない。
# 前提:
#   docker compose --profile core up -d --wait
#   ./scripts/run-script-worker.sh       # EPISODE_ID 未指定なら台本工程も走らせる
#   ./scripts/run-storyboard-worker.sh   # 別ターミナル（ホストプロセス）
#
# 検証: storyboard_ready / StoryboardArtifact 契約 / 読み戻し sha256 == metadata /
#       source_script.sha256 == 現行台本の sha256 / 再POSTで job skipped かつ同じ Artifact。
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API:-http://localhost:8000}"
VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="../../.venv"

json_field() { python3 -c "import json,sys; print(json.load(sys.stdin)$1)"; }

wait_for_status() {  # $1=episode_id $2=期待状態 → 最終 body を stdout
  local body="" status=""
  for _ in $(seq 1 180); do
    body=$(curl -sS -f "$API/episodes/$1")
    status=$(printf '%s' "$body" | json_field '["status"]')
    echo "status=$status" >&2
    case "$status" in
      "$2") printf '%s' "$body"; return 0 ;;
      failed|blocked|cancelled|needs_work)
        printf '%s' "$body" | python3 -m json.tool >&2
        echo "NG: episode ended in $status" >&2
        return 1 ;;
    esac
    sleep 5
  done
  echo "NG: timed out waiting for $2 (status=$status)" >&2
  return 1
}

if [ -z "${EPISODE_ID:-}" ]; then
  echo "== EPISODE_ID 未指定: 台本工程から走らせる（smoke-script.sh） =="
  script_out=$(./scripts/smoke-script.sh | tee /dev/stderr)
  EPISODE_ID=$(printf '%s' "$script_out" | python3 -c '
import re, sys
m = re.search(r"\"id\":\s*\"([0-9a-f-]{36})\"", sys.stdin.read())
print(m.group(1)) if m else sys.exit("NG: smoke-script.sh の出力に episode id が無い")')
fi

echo "== POST /episodes/$EPISODE_ID/storyboard =="
curl -sS -f -X POST "$API/episodes/$EPISODE_ID/storyboard" | python3 -m json.tool

echo "== storyboard_ready を待つ（数分かかる） =="
body=$(wait_for_status "$EPISODE_ID" storyboard_ready)
printf '%s' "$body" | python3 -m json.tool

verify() {  # $1=body → "object_key sha256 storyboard_job_count skipped_count"
  printf '%s' "$1" | python3 -c '
import json, sys
body = json.load(sys.stdin)
arts = [a for a in body["artifacts"] if a["artifact_type"] == "storyboard"]
if not arts: sys.exit("NG: storyboard artifact metadata が無い")
# 現行世代 = 最後に作られたもの（API は superseded_at を出さないので created_at 順の末尾）
cur = arts[-1]
scripts = [a for a in body["artifacts"] if a["artifact_type"] == "script"]
jobs = [j for j in body["jobs"] if j["type"] == "plan_storyboard"]
print(cur["object_key"], cur["sha256"], scripts[-1]["sha256"], len(jobs),
      sum(1 for j in jobs if j["status"] == "skipped"), jobs[-1]["status"])'
}

read -r object_key sb_sha script_sha _jobs1 _skipped1 _last1 <<<"$(verify "$body")"

echo
echo "== MinIO から読み戻して検証 =="
SMOKE_OBJECT_KEY="$object_key" SMOKE_EXPECTED_SHA="$sb_sha" SMOKE_SCRIPT_SHA="$script_sha" \
SMOKE_EPISODE_ID="$EPISODE_ID" \
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}" \
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}" \
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}" \
MINIO_BUCKET="${MINIO_BUCKET:-artifacts}" \
"$VENV/bin/python" - <<'PY'
import asyncio, os, sys
from contracts.artifacts import StoryboardArtifact, parse_artifact
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.storage.minio_store import MinioArtifactStore

async def main() -> None:
    store = MinioArtifactStore.from_settings(Settings())
    key = os.environ["SMOKE_OBJECT_KEY"]
    if not await store.exists(key):
        sys.exit("NG: storyboard artifact is not retrievable: " + key)
    payload = await store.get_json(key)
    artifact = parse_artifact(payload)
    if not isinstance(artifact, StoryboardArtifact):
        sys.exit("NG: parse_artifact が StoryboardArtifact を返さない")
    if artifact.episode_id != os.environ["SMOKE_EPISODE_ID"]:
        sys.exit("NG: episode_id mismatch")
    digest = sha256_hex(canonical_json_bytes(payload))
    if digest != os.environ["SMOKE_EXPECTED_SHA"]:
        sys.exit("NG: sha256 mismatch readback=" + digest)
    if artifact.source_script.sha256 != os.environ["SMOKE_SCRIPT_SHA"]:
        sys.exit("NG: source_script.sha256 != current script sha256")
    print("OK: storyboard validated")
    print("  sha256          :", digest)
    print("  source_script   :", artifact.source_script.sha256)
    print("  scenes          :", len(artifact.scenes), "/ total", artifact.total_duration_ms, "ms")
    print("  generation_spec :", artifact.metadata.generation_spec_id)

asyncio.run(main())
PY

echo
echo "== 再POST: 同じ入力なら生成器を呼ばず job skipped・同じ Artifact（INV-17） =="
curl -sS -f -X POST "$API/episodes/$EPISODE_ID/storyboard" >/dev/null
sleep 3
body2=$(wait_for_status "$EPISODE_ID" storyboard_ready)
for _ in $(seq 1 60); do
  read -r key2 sha2 _s jobs2 skipped2 last2 <<<"$(verify "$body2")"
  [ "$last2" = "skipped" ] && break
  sleep 2
  body2=$(wait_for_status "$EPISODE_ID" storyboard_ready)
done
[ "$last2" = "skipped" ] || { echo "NG: 再実行の job が skipped でない（$last2）" >&2; exit 1; }
[ "$sha2" = "$sb_sha" ] && [ "$key2" = "$object_key" ] \
  || { echo "NG: 再実行で Artifact が変わった（$sha2）" >&2; exit 1; }
echo "OK: rerun skipped (jobs=$jobs2 skipped=$skipped2) and reused sha256=$sha2"
echo
echo "OK: real Codex generated a validated storyboard artifact"
