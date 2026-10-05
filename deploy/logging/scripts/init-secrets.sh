#!/usr/bin/env bash
# ログ基盤の秘密（パスワードと PKI）を repo の外に作る（ADR-0040 §6）。ホストで実行する。
#
#   deploy/logging/scripts/init-secrets.sh [--env prod|test] [--dir DIR] [--rotate-node-cert]
#
# 既存のファイルは上書きしない（冪等）。置き場所の既定は ~/.config/avp-logging/<env>（0700、各ファイル 0600）:
#   fluentbit.pw / viewer.pw / dashboards.pw / dashboards-cookie.pw   各ユーザーのパスワード
#   fluent-bit-secret.yaml                                          Fluent Bit が include する env（writer）
#   pki/ca.{pem,key} pki/node.{pem,key} pki/admin.{pem,key}          自前 CA・ノード・admin 証明書
# admin 証明書は bootstrap / securityadmin の one-shot にだけ mount する。
# 証明書の生成はホストの openssl で行う（OpenSearch / Dashboards のイメージに openssl が無い）。
# DN は RFC2253 で "CN=…,OU=avp2-logging" になるよう -subj は OU を先に書く（opensearch.yml と一致させる）。
set -euo pipefail

ENV_NAME="${AVP_LOGGING_ENV:-prod}"
DIR=""
ROTATE_NODE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV_NAME="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --rotate-node-cert) ROTATE_NODE=1; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
DIR="${DIR:-${AVP_LOGGING_SECRETS_DIR:-$HOME/.config/avp-logging/$ENV_NAME}}"

command -v openssl >/dev/null || { echo "openssl が必要" >&2; exit 1; }
umask 077
mkdir -p "$DIR/pki"
chmod 700 "$DIR" "$DIR/pki"

new_password() {
  local path="$1"
  if [[ ! -s "$path" ]]; then
    # 記号を避け、YAML・HTTP Basic でそのまま扱える 32 文字（128 bit）
    openssl rand -hex 16 | tr -d '\n' >"$path"
    echo "created $path"
  fi
  chmod 600 "$path"
}
for user in fluentbit viewer dashboards dashboards-cookie; do
  new_password "$DIR/$user.pw"
done

secret_yaml="$DIR/fluent-bit-secret.yaml"
if [[ ! -s "$secret_yaml" ]]; then
  printf 'env:\n  AVP_OS_WRITER_PASSWORD: "%s"\n' "$(cat "$DIR/fluentbit.pw")" >"$secret_yaml"
  echo "created $secret_yaml"
fi
chmod 600 "$secret_yaml"

pki="$DIR/pki"
key() { openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 2>/dev/null | openssl pkcs8 -topk8 -nocrypt -out "$1"; }

if [[ ! -s "$pki/ca.pem" ]]; then
  key "$pki/ca.key"
  openssl req -x509 -new -key "$pki/ca.key" -sha256 -days 3650 \
    -subj "/OU=avp2-logging/CN=avp2-logging-ca" -out "$pki/ca.pem"
  echo "created $pki/ca.pem"
fi

issue() {
  local name="$1" cn="$2" ext="$3"
  key "$pki/$name.key"
  local extfile
  extfile="$(mktemp)"
  printf '%b' "$ext" >"$extfile"
  openssl req -new -key "$pki/$name.key" -subj "/OU=avp2-logging/CN=$cn" \
    | openssl x509 -req -CA "$pki/ca.pem" -CAkey "$pki/ca.key" -CAcreateserial \
      -days 825 -sha256 -extfile "$extfile" -out "$pki/$name.pem" 2>/dev/null
  rm -f "$extfile"
  echo "created $pki/$name.pem"
}

if [[ ! -s "$pki/node.pem" || $ROTATE_NODE -eq 1 ]]; then
  # SAN: compose のサービス名（Fluent Bit / Dashboards が名前で検証する）と loopback（ホストの確認用）
  issue node opensearch \
    "subjectAltName=DNS:opensearch,DNS:localhost,IP:127.0.0.1\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n"
fi
if [[ ! -s "$pki/admin.pem" ]]; then
  issue admin avp2-logging-admin \
    "basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage=clientAuth\n"
fi
chmod 600 "$pki"/*

echo "secrets: $DIR"
echo "node DN:  $(openssl x509 -in "$pki/node.pem" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
echo "admin DN: $(openssl x509 -in "$pki/admin.pem" -noout -subject -nameopt RFC2253 | sed 's/^subject=//')"
