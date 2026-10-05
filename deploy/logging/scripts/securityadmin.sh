#!/usr/bin/env bash
# one-shot（setup profile、OpenSearch のイメージ）: security 設定の変更を稼働中の cluster へ反映する。
# 初回は allow_default_init_securityindex で自動的に入るので不要。パスワード・ロールを変えたときに使う:
#   1) init-secrets.sh でパスワードを変える（ファイルを消して再生成）
#   2) compose run --rm security-init   （internal_users.yml を作り直す）
#   3) compose run --rm securityadmin   （この script）
# admin 証明書はこの one-shot にだけ mount する。
set -euo pipefail
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
# 既定ファイル（action_groups・tenants 等）をイメージから取り、自前のもので上書きする
cp /usr/share/opensearch/config/opensearch-security/*.yml "$work/"
cp /deploy/security/config.yml /deploy/security/roles.yml /deploy/security/roles_mapping.yml "$work/"
cp /osconf/security/internal_users.yml "$work/"
OPENSEARCH_JAVA_OPTS="-Xmx128m" bash /usr/share/opensearch/plugins/opensearch-security/tools/securityadmin.sh \
  -cd "$work" -icl -h "${OPENSEARCH_HOST:-opensearch}" \
  -cacert /secrets/pki/ca.pem -cert /secrets/pki/admin.pem -key /secrets/pki/admin.key
