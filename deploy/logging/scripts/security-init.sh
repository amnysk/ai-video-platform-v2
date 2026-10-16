#!/usr/bin/env bash
# one-shot（compose の setup profile、OpenSearch のイメージ、コンテナ内 root）:
# repo 外の秘密から OpenSearch 用の証明書と internal_users.yml を named volume に置き、
# 各 volume に sentinel を書く（ADR-0040 §6/§7）。
#
# mount（compose.logging.yaml）:
#   /secrets (ro)                    init-secrets.sh の出力
#   /tmpl (ro)                       deploy/logging/opensearch/security
#   /osconf                          named volume: certs/ と security/internal_users.yml
#   /usr/share/opensearch/data       named volume: OpenSearch のデータ（所有者 1000 をイメージから継ぐ）
#   /fb-state                        named volume: Fluent Bit の位置 DB と buffer
#
# 再実行しても壊れない。パスワードを変えたら再実行し、securityadmin を流す。
set -euo pipefail

SENTINEL=".avp-logging-sentinel"
OS_UID=1000

for f in pki/ca.pem pki/node.pem pki/node.key fluentbit.pw viewer.pw dashboards.pw; do
  [[ -s "/secrets/$f" ]] || { echo "missing /secrets/$f (run init-secrets.sh first)" >&2; exit 1; }
done

mkdir -p /osconf/certs /osconf/security
install -m 0644 /secrets/pki/ca.pem /osconf/certs/ca.pem
install -m 0644 /secrets/pki/node.pem /osconf/certs/node.pem
install -m 0600 /secrets/pki/node.key /osconf/certs/node.key

hash_of() {
  # パスワードは子プロセスの env だけに置く（コンテナの Env・コマンド行には出さない）
  PW="$(cat "/secrets/$1.pw")" OPENSEARCH_JAVA_OPTS="-Xmx128m" \
    bash /usr/share/opensearch/plugins/opensearch-security/tools/hash.sh -env PW 2>/dev/null | tail -n 1
}
h_fb="$(hash_of fluentbit)"
h_viewer="$(hash_of viewer)"
h_osd="$(hash_of dashboards)"
for h in "$h_fb" "$h_viewer" "$h_osd"; do
  [[ "$h" == '$2'* ]] || { echo "hash.sh failed" >&2; exit 1; }
done
tmp="$(mktemp)"
sed -e "s|__HASH_FLUENTBIT__|${h_fb}|" -e "s|__HASH_VIEWER__|${h_viewer}|" \
  -e "s|__HASH_DASHBOARDS__|${h_osd}|" /tmpl/internal_users.yml.tmpl >"$tmp"
if grep -q '__HASH_' "$tmp"; then echo "template not fully rendered" >&2; exit 1; fi
install -m 0600 "$tmp" /osconf/security/internal_users.yml
rm -f "$tmp"

touch "/osconf/$SENTINEL" "/usr/share/opensearch/data/$SENTINEL" "/fb-state/$SENTINEL"
chmod 0644 "/osconf/$SENTINEL" "/usr/share/opensearch/data/$SENTINEL" "/fb-state/$SENTINEL"
chown -R "$OS_UID:$OS_UID" /osconf /usr/share/opensearch/data
echo "security-init: certs and internal_users.yml installed; sentinels written"
