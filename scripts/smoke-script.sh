#!/usr/bin/env bash
# 本物の Codex で台本を1本生成し、MinIO から読み戻して検証する。
#
# **外部AI呼び出し（課金）を伴う。** 通常の pytest / CI からは走らない。
# 前提:
#   docker compose --profile core up -d --wait
#   ./scripts/run-script-worker.sh   # 別ターミナル（ホストプロセス）
set -euo pipefail
cd "$(dirname "$0")/.."

API="${API:-http://localhost:8000}"
TOPIC="${TOPIC:-縄文土器の焦げ跡が語る食生活}"
VENV="${VENV:-.venv}"
[ -x "$VENV/bin/python" ] || VENV="../../.venv"

echo "== POST /episodes (pipeline=script) =="
created=$(curl -sS -f -X POST "$API/episodes" \
  -H 'content-type: application/json' \
  -d "$(printf '{"topic":%s,"pipeline":"script"}' "$(printf '%s' "$TOPIC" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")")
echo "$created"
episode_id=$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo
echo "== GET /episodes/$episode_id (script_ready を待つ。Codexは数分かかる) =="
body=""
for _ in $(seq 1 120); do
  body=$(curl -sS "$API/episodes/$episode_id")
  status=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "status=$status"
  case "$status" in
    script_ready) break ;;
    failed|blocked|cancelled)
      printf '%s' "$body" | python3 -m json.tool
      echo "NG: episode ended in $status" >&2
      exit 1
      ;;
  esac
  sleep 5
done

status=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
[ "$status" = "script_ready" ] || { echo "NG: timed out (status=$status)" >&2; exit 1; }
printf '%s' "$body" | python3 -m json.tool

echo
echo "== MinIO から読み戻して検証（schema / episode_id / schema_version / SHA-256）=="
object_key=$(printf '%s' "$body" | python3 -c '
import json,sys
arts=[a for a in json.load(sys.stdin)["artifacts"] if a["artifact_type"]=="script"]
if not arts: sys.exit("NG: script artifact metadata が無い")
print(arts[0]["object_key"])')
expected_sha=$(printf '%s' "$body" | python3 -c '
import json,sys
print([a for a in json.load(sys.stdin)["artifacts"] if a["artifact_type"]=="script"][0]["sha256"])')

SMOKE_OBJECT_KEY="$object_key" SMOKE_EXPECTED_SHA="$expected_sha" \
SMOKE_EPISODE_ID="$episode_id" \
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}" \
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}" \
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-change-me}" \
MINIO_BUCKET="${MINIO_BUCKET:-artifacts}" \
"$VENV/bin/python" - <<'PY'
import asyncio, os, sys
from contracts.artifacts import SCRIPT_ARTIFACT_SCHEMA_VERSION, ScriptArtifact, parse_artifact
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.config import Settings
from infrastructure.storage.minio_store import MinioArtifactStore

async def main() -> None:
    store = MinioArtifactStore.from_settings(Settings())
    key = os.environ["SMOKE_OBJECT_KEY"]
    if not await store.exists(key):
        sys.exit("NG: script artifact is not retrievable: " + key)
    payload = await store.get_json(key)

    artifact = parse_artifact(payload)
    if not isinstance(artifact, ScriptArtifact):
        sys.exit("NG: parse_artifact が ScriptArtifact を返さない")
    if artifact.episode_id != os.environ["SMOKE_EPISODE_ID"]:
        sys.exit("NG: episode_id mismatch")
    if artifact.schema_version != SCRIPT_ARTIFACT_SCHEMA_VERSION:
        sys.exit("NG: schema_version mismatch")
    digest = sha256_hex(canonical_json_bytes(payload))
    if digest != os.environ["SMOKE_EXPECTED_SHA"]:
        sys.exit("NG: sha256 mismatch stored=" + digest)

    print("OK: schema validation passed")
    print("  episode_id     :", artifact.episode_id)
    print("  schema_version :", artifact.schema_version)
    print("  sha256         :", digest)
    print("  title          :", artifact.title)
    print("  hook           :", artifact.hook)
    print("  scenes         :", len(artifact.scenes), "/ total", artifact.total_duration_ms, "ms")

asyncio.run(main())
PY

echo
echo "OK: real Codex generated a validated script artifact"
