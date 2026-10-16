#!/usr/bin/env bash
# 稼働中のアプリコンテナ（この repo からビルドしたイメージ）の image id と git revision を表示する。
# 次のどれかがあれば exit 1（docs/operations/workers.md §10）:
#   - コンテナの image id が現在のタグ（avp2-app:local / avp2-worker:local）と違う（STALE = 作り直されていない）
#   - revision が読めない（label が無い / unknown。素の `docker compose build` で作った、または古いイメージ）
#   - コンテナ間で revision が2種類以上ある
#   - EXPECTED_REVISION が設定されていて、それと違う（deploy-workers.sh が渡す）
# docker の読み取りだけで、何も変更しない。
set -euo pipefail
cd "$(dirname "$0")/.."

LABEL=org.opencontainers.image.revision
status=0
revisions=()
printf '%-28s %-20s %-14s %-44s %s\n' SERVICE IMAGE IMAGE_ID REVISION STATE
for cid in $(docker compose --profile core ps -a -q); do
  read -r service image id revision <<<"$(docker inspect --format \
    "{{index .Config.Labels \"com.docker.compose.service\"}} {{.Config.Image}} {{.Image}} {{with index .Config.Labels \"$LABEL\"}}{{.}}{{else}}-{{end}}" "$cid")"
  case "$image" in avp2-app:local | avp2-worker:local) ;; *) continue ;; esac
  current="$(docker image inspect --format '{{.Id}}' "$image" 2>/dev/null || echo missing)"
  state=ok
  if [ "$id" != "$current" ]; then state="STALE (tag now ${current:7:12})"; status=1; fi
  case "$revision" in
    - | unknown) state="$state NO-REVISION"; status=1 ;;
  esac
  revisions+=("$revision")
  printf '%-28s %-20s %-14s %-44s %s\n' "$service" "$image" "${id:7:12}" "$revision" "$state"
done

kinds="$(printf '%s\n' "${revisions[@]}" | sort -u | grep -c . || true)"
if [ "${kinds:-0}" -gt 1 ]; then
  echo "NG: 稼働中コンテナの revision が ${kinds} 種類ある" >&2
  status=1
fi
if [ -n "${EXPECTED_REVISION:-}" ]; then
  for r in "${revisions[@]}"; do
    if [ "$r" != "$EXPECTED_REVISION" ]; then
      echo "NG: revision $r は期待する $EXPECTED_REVISION と違う" >&2
      status=1
      break
    fi
  done
fi
if [ "${#revisions[@]}" -eq 0 ]; then
  echo "NG: アプリコンテナが1つも見つからない" >&2
  status=1
fi
if [ "$status" -eq 0 ]; then
  echo "OK: 全アプリコンテナが現在のイメージ・同じ revision で動いている"
else
  echo "NG: 版が揃っていない。scripts/deploy-workers.sh（make deploy-workers）" >&2
fi
exit "$status"
