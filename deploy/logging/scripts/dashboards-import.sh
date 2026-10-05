#!/usr/bin/env bash
# one-shot（dashboards-setup profile）: index pattern と保存済み検索を import する（overwrite で冪等）。
# 閲覧者（kibana_user）で入れる。
set -euo pipefail
url="${DASHBOARDS_URL:-http://dashboards:5601}"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "avp_viewer:$(cat /secrets/viewer.pw)" "$url/api/status" || true)"
  [[ "$code" == 200 ]] && break
  sleep 5
done
curl -sS --fail-with-body -u "avp_viewer:$(cat /secrets/viewer.pw)" -H 'osd-xsrf: true' \
  -X POST "$url/api/saved_objects/_import?overwrite=true" \
  --form file=@/deploy/dashboards/saved-objects.ndjson
echo
