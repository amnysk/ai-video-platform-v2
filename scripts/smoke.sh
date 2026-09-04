#!/usr/bin/env bash
# 起動中のスタックに対して Episode を1本流し、completed になるまで見届ける。
# 使い方: docker compose --profile core up -d && ./scripts/smoke.sh
set -euo pipefail

API="${API:-http://localhost:8000}"

echo "== POST /episodes =="
created=$(curl -sS -X POST "$API/episodes" \
  -H 'content-type: application/json' \
  -d '{"topic":"smoke test"}')
echo "$created"

episode_id=$(printf '%s' "$created" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')

echo
echo "== GET /episodes/$episode_id (completed になるまで待つ) =="
for _ in $(seq 1 60); do
  body=$(curl -sS "$API/episodes/$episode_id")
  status=$(printf '%s' "$body" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  echo "status=$status"
  case "$status" in
    completed)
      printf '%s' "$body" | python3 -m json.tool
      echo "OK: episode completed"
      exit 0
      ;;
    failed|blocked|cancelled)
      printf '%s' "$body" | python3 -m json.tool
      echo "NG: episode ended in $status" >&2
      exit 1
      ;;
  esac
  sleep 1
done

echo "NG: timed out waiting for completion" >&2
exit 1
