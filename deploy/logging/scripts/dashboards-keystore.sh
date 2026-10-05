#!/usr/bin/env bash
# one-shot（dashboards-setup profile、Dashboards のイメージ、コンテナ内 root）:
# サーバーユーザーのパスワードと cookie の鍵を keystore に入れ、named volume に置く（env では渡さない）。
set -euo pipefail
cd /usr/share/opensearch-dashboards
rm -f config/opensearch_dashboards.keystore
bin/opensearch-dashboards-keystore create --allow-root >/dev/null
bin/opensearch-dashboards-keystore add opensearch.password --stdin --allow-root </secrets/dashboards.pw >/dev/null
bin/opensearch-dashboards-keystore add opensearch_security.cookie.password --stdin --allow-root \
  </secrets/dashboards-cookie.pw >/dev/null
install -m 0600 -o 1000 -g 1000 config/opensearch_dashboards.keystore /osdconf/opensearch_dashboards.keystore
touch /osdconf/.avp-logging-sentinel
echo "dashboards-keystore: ok"
