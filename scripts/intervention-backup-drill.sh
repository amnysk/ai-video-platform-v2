#!/usr/bin/env bash
# 判断記録と介入の backup/restore 演習（設計 §14・§17-11。docs/operations/intervention-runbook.md §5）。
#
# **隔離スタック専用**（deploy/logging/test/compose.apptest.yaml、project `avp2-oslog-c*`）。
# 本番 project `avp2` は拒否する。先に `deploy/logging/test/run-e2e.sh build && ... up` を済ませること。
#
#   AVP_APPTEST_PROJECT=avp2-oslog-c-jpmasd scripts/intervention-backup-drill.sh [--keep]
#
# 1. 元（隔離の app DB `avp` + bucket `artifacts`）に Episode 1本ぶんの Artifact と介入の行を足す
# 2. pg_dump -Fc avp → 状態ディレクトリ（repo 外）
# 3. 新しい DB `avp_restore` へ pg_restore、MinIO を `artifacts` → `artifacts-restore` へ mirror
# 4. 復元先に向けて scripts/consistency-check.py → 0（整合）であること
# 5. 復元先から1オブジェクト消して再検査 → 1（不整合を検出する）こと
# 6. `--keep` が無ければ復元先の DB と bucket を消す
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERE="$ROOT/deploy/logging/test"
PROJECT="${AVP_APPTEST_PROJECT:?AVP_APPTEST_PROJECT を指定する（avp2-oslog-c*）}"
case "$PROJECT" in
  avp2-oslog-c|avp2-oslog-c[-_0-9a-z]*) ;;
  *) echo "NG: AVP_APPTEST_PROJECT=$PROJECT は許可しない（本番 avp2 を守る）" >&2; exit 2 ;;
esac
STATE="${AVP_APPTEST_STATE:-${XDG_RUNTIME_DIR:-/tmp}/avp2-oslog-apptest/$PROJECT}"
ENV_FILE="$STATE/apptest.env"
[ -s "$ENV_FILE" ] || { echo "NG: $ENV_FILE が無い。先に run-e2e.sh up" >&2; exit 2; }
KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
RESTORE_DB=avp_restore
RESTORE_BUCKET=artifacts-restore
DRILL="$STATE/drill"; install -d -m 700 "$DRILL"

dc() {
  # compose run の one-off は orphan 警告を出すだけ（消さない）
  install -d -m 700 "$STATE/empty"
  APPTEST_SECRETS_DIR="$STATE/empty" APPTEST_SRC="$ROOT" \
  APPTEST_RUNNER_IMAGE="${APPTEST_RUNNER_IMAGE:-avp2-oslog-c-runner:test}" \
  APPTEST_GIT_REVISION="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)" \
  AVP_APPTEST_PROJECT="$PROJECT" \
    docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f "$HERE/compose.apptest.yaml" "$@"
}
# パスワードはファイルから読むだけ（表示しない）
# shellcheck disable=SC1090
. "$ENV_FILE"
RESTORE_URL="postgresql+psycopg://avp:${APPTEST_PG_PASSWORD}@postgres:5432/${RESTORE_DB}"
step() { echo "[$(( $(date +%s) - T0 ))s] $*"; }
T0=$(date +%s)

check() {  # 復元先へ向けた整合検査。consistency-check の終了コードを返す
  dc --profile runner run -T --rm --no-deps \
    -e DATABASE_URL="$RESTORE_URL" -e MINIO_BUCKET="$RESTORE_BUCKET" \
    test-runner python scripts/consistency-check.py "$@" >"$DRILL/check.out" 2>&1 && rc=0 || rc=$?
  grep -v 'orphan' "$DRILL/check.out" | tail -5
  return "$rc"
}

step "1. seed the source (isolated avp / artifacts)"
dc --profile runner run -T --rm --no-deps test-runner python scripts/intervention-backup-drill-seed.py

step "2. pg_dump avp"
dc exec -T postgres pg_dump -U avp -Fc avp >"$DRILL/avp.dump"
ls -l "$DRILL/avp.dump" | awk '{print "   dump bytes=" $5}'

step "3a. restore into $RESTORE_DB"
dc exec -T postgres dropdb -U avp --if-exists "$RESTORE_DB"
dc exec -T postgres createdb -U avp "$RESTORE_DB"
dc exec -T postgres pg_restore -U avp -d "$RESTORE_DB" --no-owner --exit-on-error <"$DRILL/avp.dump"

step "3b. mirror bucket artifacts -> $RESTORE_BUCKET"
dc exec -T minio sh -c '
  mc alias set drill http://localhost:9000 apptest "$MINIO_ROOT_PASSWORD" >/dev/null
  mc rb --force drill/'"$RESTORE_BUCKET"' >/dev/null 2>&1 || true
  mc mb drill/'"$RESTORE_BUCKET"' >/dev/null
  mc mirror --quiet drill/artifacts drill/'"$RESTORE_BUCKET"' >/dev/null
  echo "   objects source=$(mc ls --recursive drill/artifacts | wc -l) restored=$(mc ls --recursive drill/'"$RESTORE_BUCKET"' | wc -l)"
'

step "4. consistency-check against the restored copy (expect 0)"
if check; then step "   OK: restored copy is consistent"; else echo "NG: restored copy is inconsistent" >&2; exit 1; fi

step "5. remove one restored object and re-check (expect 1)"
# minio のイメージには grep/awk が無いので、一覧だけ取ってホスト側で選ぶ
victim="$(dc exec -T minio mc ls --recursive "drill/$RESTORE_BUCKET" | awk '{print $NF}' | grep '/script/' | head -1)"
[ -n "$victim" ] || { echo "NG: no script object to remove" >&2; exit 1; }
echo "   removing $victim"
dc exec -T minio mc rm --quiet "drill/$RESTORE_BUCKET/$victim" >/dev/null
rc=0; check || rc=$?
if [ "$rc" -eq 1 ]; then step "   OK: the missing object was detected"; else echo "NG: expected rc=1, got $rc" >&2; exit 1; fi

if [ "$KEEP" -eq 0 ]; then
  step "6. drop $RESTORE_DB and $RESTORE_BUCKET"
  dc exec -T postgres dropdb -U avp --if-exists "$RESTORE_DB"
  dc exec -T minio sh -c 'mc rb --force drill/'"$RESTORE_BUCKET"' >/dev/null'
  rm -f "$DRILL/avp.dump"
fi
step "drill passed"
