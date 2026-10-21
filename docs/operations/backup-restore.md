# バックアップと復旧演習の手順書

Status: **Proposed**（ADR-0051 / INV-66）。所有者が承認するまで、本番では実行しない。

> `[R]` は本番へ読み取りだけを送る（backup 自体も `[R]`: pg_dump と S3 の list/get）。
> `[D]` は使い捨ての復旧先にだけ書く。`[W]` は backup の保存先に書く／消す。
> 本番の PostgreSQL・MinIO・コンテナを再起動・再作成・書き換える手順はこの文書に無い。

## 1. 何を取るか

| 対象 | 取り方 | 実測 (2026-10-21) |
|---|---|---|
| `avp`（アプリ DB） | `docker exec avp2-postgres-1 pg_dump --format=custom` | 14 MB（alembic `0015`、`artifact_metadata` 592 行） |
| `temporal` / `temporal_visibility`（Temporal は同じ PG 内） | 同上 | 44 MB / 43 MB |
| ロール（`globals.sql`） | `pg_dumpall --globals-only`（パスワードハッシュを含む。0400） | 数 KB |
| MinIO bucket `artifacts` | S3 API（`minio` SDK）で list → get。sha256 と etag(md5) を検査 | 約 2.1 GB |
| 対象外 | `avp_test`・`postgres`・`template*`、bucket `artifacts-test` / `artifacts-itest`、`/mnt/minio-hdd/ai-video-work`（一時領域） | — |

保存先は 2 つ（既定なし）。**守る対象と別のデバイス**に置く:
`AVP_BACKUP_DB_ROOT`（例: HDD `/mnt/minio-hdd/avp-backups`。NVMe の pgdata を守る）と
`AVP_BACKUP_OBJECT_ROOT`（例: NVMe 上の `~/avp-backups`。HDD の MinIO を守る）。同じデバイスなら拒否される。
両方に同じ manifest が置かれる。**これはホスト全体の喪失は防がない**（§7 offsite）。

## 2. 一貫性の境界

PostgreSQL（3 DB）→ MinIO の順。Artifact は不変（INV-11）で DB の行は本体の put の後に commit されるので、
dump が指す本体は後から取る MinIO の写しに在る。manifest の `consistency` に両方の開始・終了時刻が残る。
alembic revision が取得中に変わったら失敗する。静止（maintenance pause）は既定で使わない:
`scripts/with-maintenance-pause.sh` は失敗時に pause を**解除しない**ので、無人の定時実行に包むと
backup の失敗が Daily の停止になる。手動で取るときに限り、所有者の判断で
`--quiesce-note "paused by <name> at <time>"` と併用できる（記録のみで証明ではない）。

## 2.5 実行前の点検（`[R]`、何も作らない）

```bash
python scripts/backup.py --db-root … --object-root … --env-file … precheck   # JSON(stdout) + 要約(stderr)
```

保存先・容量（取得元 x 2）・PG/MinIO の到達性とバージョン・認証情報の有無（名前のみ）・alembic・Schedule の窓を
PASS/FAIL/UNKNOWN で出す。root が無くても作らず、audit も残さない。保存先が `MINIO_DATA_DIR` 等の保護パスと
（symlink 解決後に）重なる場合は `--allow-same-device` でも拒否される。結果と承認手順:
`docs/operations/production-recovery-preapproval.md`。

## 3. backup を取る（`[R]` + `[W]`）

```bash
# 設定（例）。--env-file などの共通引数はサブコマンドの前
export AVP_BACKUP_DB_ROOT=/mnt/minio-hdd/avp-backups  AVP_BACKUP_OBJECT_ROOT=$HOME/avp-backups
python scripts/backup.py --env-file /path/to/prod/.env run
echo $?          # 0 成功 / 1 失敗（audit.log と stderr に backup.failed、通知は設定時のみ）
```

- 成功すると `<root>/<backup_id>/`（UTC 時刻）が両 root に確定し、`manifest.json` は `restore_verified: false`
- 失敗すると自分の `*.partial` を消し、確定した集合は作らない。deploy / migration 中は走らせない
  （`pg_dump` は書き込みを止めないが DDL と競合する。alembic が変われば失敗する）
- 前回と同じ不変オブジェクトは hardlink で共有する（増分）。ファイルは 0400
- release preflight へは `--backup-manifest <root>/<backup_id>/manifest.json` を渡す
  （`components.{db_dump,minio,temporal}.created_at` を読む。互換は contract test が保証）

## 4. 復旧演習（`[D]`）— これだけが `restore_verified` を true にする

```bash
python scripts/backup.py restore-drill --backup-id latest --work-root "$HOME/.cache/avp-restore"
echo $?          # 0 pass / 1 fail / 2 拒否（復旧先が本番など）
```

使い捨ての `postgres:17-alpine`（名前 `avp-restore-*`、`--network none`、ポート非公開、DB 名 `restore_*`）に
`pg_restore` し、作業ディレクトリへ全ファイルを複写して検証する:

1. 全ファイルの sha256・サイズが manifest と一致（壊れていれば**復旧せずに**失敗）
2. 3 DB が戻る
3. 行数が backup 時の `[before, after]` に入る
4. alembic revision が一致
5. オブジェクトの sha256
6. `artifact_metadata` / `research_artifacts` の全行の `(bucket, object_key, sha256)` が、復旧した本体と一致

結果（各検査・時刻・復旧先・sha256）が両 root の manifest の `restore_drill` に入る。**fail すると、以前
`restore_verified: true` だった集合も false に戻る**（bit-rot の検出）。終了後にコンテナと作業ディレクトリは消える
（消えなければ `restore.cleanup_failed` と終了コード 1）。復旧先が本番（container / project / host / port / DB 名 /
bucket 名 / 作業パス / backup root と重なる）なら、docker を 1 度も呼ばずに終了コード 2 で拒否する。

`python scripts/backup.py verify --backup-id <id>` は restore せず全ファイルを再ハッシュする（hardlink は全世代に
共有されるので、定期的に）。`status` は一覧と鮮度（`--max-age-hours` を超えると 1）。

## 5. 保持（`[W]`）

```bash
python scripts/backup.py prune            # dry-run（既定）。計画だけ表示
python scripts/backup.py prune --apply    # 検証済みで方針外の集合だけを削除
```

既定: 日 7 / 週 4 / 月 6。**未検証・未完了の集合は消さない。最新の検証済み 1 つも消さない。** 検証済みが 1 つも
無ければ警告を出す（`no restore-verified backup exists`）。削除は `<root>` 直下の `backup_id` 形のディレクトリ
（symlink でない）だけで、削除直前に全 root の manifest を読み直す。古い `/mnt/minio-hdd/avp-db-backups` の
手作業 dump は触らない（所有者が判断）。

## 6. 本番を復旧するとき（自動化しない）

この repo は本番への restore を実装しない。本番の復旧は所有者の決定と立ち会いで行う:

1. 自動生成を止める（maintenance pause を解除しない、DB スイッチ `paused` / `uploads_paused`）
2. 最新の `restore_verified: true` の集合を選び、`verify` を通す
3. drill と同じ手順を**本番の停止中の PG / MinIO**に対して手で行う（pg_restore、オブジェクトの書き戻し）
4. Temporal はアプリ DB と別スナップショットなので履歴が食い違いうる。権威はアプリ DB（INV-7 / INV-8）
5. `reserved` のまま残る provider 予約は照合するまで**再送しない**（INV-15 / ADR-0013）。YouTube の既存投稿は触らない

## 7. offsite（任意・既定オフ）

`AVP_BACKUP_OFFSITE_CMD`（argv。`{db_set}` `{object_set}` が置換される）を設定したときだけ、確定した集合を
送る。**設定しなければ外部送信は一切しない。** 送信先・資格情報・暗号化は所有者が決める（推奨: restic。
`globals.sql` はロールのパスワードハッシュを含むので暗号化なしで送らない）。失敗すると終了コード 1
（ローカルの集合は残り、manifest の `offsite.ok` が false）。

## 8. 定時実行

`deploy/backup/` に systemd user unit / crontab の**例**がある（install しない。所有者が承認して行う）。
毎日 `run`、週 1 回 `restore-drill` + `prune --apply`、`status --max-age-hours 26` を監視に。

## 9. 未実装・既知の穴

- PITR は無い（RPO = 取得間隔）。必要になったら pgBackRest を別の ADR で
- release preflight は `restore_verified` を**まだ要求しない**（別の変更・別の承認）
- `RealCollectors` 同様、本番に対する初回の `run` は所有者の立ち会いで行い、結果を記録する
- drill は MinIO サーバーを起動しない（オブジェクトはファイルとして検証）。MinIO の内部形式から戻す手順ではない
- MinIO のバージョニング有無は manifest の `objects.buckets.<bucket>.versioning` に記録するだけ（最新版のみ複写）
