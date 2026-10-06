#!/usr/bin/env bash
# one-shot（dashboards-setup profile）: index pattern と保存済み検索を import する（overwrite で冪等）。
# 閲覧者（kibana_user）で入れる。
set -euo pipefail
url="${DASHBOARDS_URL:-http://dashboards:5601}"
# パスワードを curl の argv に出さない（0600 の一時 config を -K で読ませる）
cfg="$(mktemp)"
trap 'rm -f "$cfg"' EXIT
chmod 600 "$cfg"
printf 'user = "avp_viewer:%s"\n' "$(cat /secrets/viewer.pw)" >"$cfg"
for _ in $(seq 1 60); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -K "$cfg" "$url/api/status" || true)"
  [[ "$code" == 200 ]] && break
  sleep 5
done
curl -sS --fail-with-body -K "$cfg" -H 'osd-xsrf: true' \
  -X POST "$url/api/saved_objects/_import?overwrite=true" \
  --form file=@/deploy/dashboards/saved-objects.ndjson
echo
