#!/usr/bin/env bash
# 起動中のスタックに対して Episode を1本流し、completed になるまで見届け、
# **保存された Artifact を実際に読み戻せること**まで確認する。
#
# 使い方: docker compose --profile core up -d --wait && ./scripts/smoke.sh
#
# 読み戻しの確認が要る理由: put が成功しても、ストレージ backend によっては
# 直後の GET / LIST が失敗することがある（docs/operations/storage-backend.md）。
# 「書けたが読めない」を smoke が緑にしてしまうと、基盤の合格判定が嘘になる。
set -euo pipefail

API="${API:-http://localhost:8000}"

echo "== POST /episodes =="
created=""
for _ in $(seq 1 30); do
  if created=$(curl -sS -f -X POST "$API/episodes" \
      -H 'content-type: application/json' \
      -d '{"topic":"smoke test"}' 2>/dev/null); then
    break
  fi
  echo "  api not ready yet..."
  sleep 1
done
if [ -z "$created" ]; then
  echo "NG: API did not accept POST /episodes" >&2
  exit 1
fi
echo "$created"

episode_id=$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo
echo "== GET /episodes/$episode_id (completed になるまで待つ) =="
body=""
for _ in $(seq 1 60); do
  body=$(curl -sS "$API/episodes/$episode_id")
  status=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "status=$status"
  case "$status" in
    completed) break ;;
    failed|blocked|cancelled)
      printf '%s' "$body" | python3 -m json.tool
      echo "NG: episode ended in $status" >&2
      exit 1
      ;;
  esac
  sleep 1
done

status=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
if [ "$status" != "completed" ]; then
  echo "NG: timed out waiting for completion (status=$status)" >&2
  exit 1
fi
printf '%s' "$body" | python3 -m json.tool

echo
echo "== Artifact を実際に読み戻せるか（書けたが読めない、を検出する）=="
object_key=$(printf '%s' "$body" | python3 -c '
import json,sys
artifacts = json.load(sys.stdin)["artifacts"]
if not artifacts:
    sys.exit("NG: episode has no artifact metadata")
print(artifacts[0]["object_key"])')
expected_sha=$(printf '%s' "$body" | python3 -c '
import json,sys; print(json.load(sys.stdin)["artifacts"][0]["sha256"])')

echo "object_key=$object_key"
docker compose run --rm --no-deps -T \
  -e SMOKE_OBJECT_KEY="$object_key" -e SMOKE_EXPECTED_SHA="$expected_sha" \
  --entrypoint python api -c '
import asyncio, os, sys
from infrastructure.config import Settings
from infrastructure.storage.minio_store import MinioArtifactStore
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from contracts.artifacts import parse_artifact

async def main() -> None:
    store = MinioArtifactStore.from_settings(Settings())
    key = os.environ["SMOKE_OBJECT_KEY"]
    expected = os.environ["SMOKE_EXPECTED_SHA"]
    if not await store.exists(key):
        sys.exit("NG: artifact is not retrievable from object storage: " + key)
    payload = await store.get_json(key)
    parse_artifact(payload)
    digest = sha256_hex(canonical_json_bytes(payload))
    if digest != expected:
        sys.exit("NG: sha256 mismatch: stored=" + digest + " recorded=" + expected)
    print("OK: artifact readable and sha256 matches (" + digest[:12] + "...)")

asyncio.run(main())
'

echo
echo "OK: episode completed and its artifact is readable"
