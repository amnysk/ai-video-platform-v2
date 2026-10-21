# 本番復旧基盤の承認前チェックと実行順序

Status: **所有者の承認待ち**（ADR-0051 / ADR-0052）。この文書は判断材料で、ここに書いた手順は**どれも実行されていない**。
実行したのは `python scripts/backup.py precheck`（読み取りだけ）と、security 観測のための `docker ps` / `ss` / `df` / `ls`
だけ。`run` / `restore-drill` / `prune --apply` は走らせていない。本番の pgdata・`/mnt/minio-hdd` へは何も書いていない。

## 1. precheck の結果（2026-10-21 01:40 UTC、本番に対して読み取りのみ）

提案値: DB root `/mnt/minio-hdd/avp-backups`、object root `$HOME/avp-backups`（= `/home/yoshiki/avp-backups`）。
**どちらもまだ存在しない**。`run` の最初に作られる（precheck は作らない）。親ディレクトリは現在のユーザーが書ける。

総合判定: **UNKNOWN**（FAIL 0 / UNKNOWN 2 / PASS 10）。UNKNOWN は「所有者が見て判断する」2 点で、技術的な阻害ではない。

| 項目 | 判定 | 実測・理由 |
|---|---|---|
| root_db | PASS | `/mnt/minio-hdd`（/dev/sda1）。守る対象 pgdata（NVMe、docker volume `avp2_pgdata`）と別デバイス。MinIO データ dir の外 |
| root_objects | PASS | `/home/yoshiki`（NVMe）。守る対象 MinIO（`/mnt/minio-hdd/minio-data`、HDD）と別デバイス |
| postgres_version | PASS | pg_dump 17 >= server 17 |
| postgres_databases | PASS | `avp` 15.0 MB / `temporal` 46.5 MB / `temporal_visibility` 44.7 MB。他に `avp_test`, `postgres` |
| postgres_activity | PASS | 実行中セッション 0、長い DDL なし、AccessExclusiveLock なし |
| minio_bucket | PASS | `artifacts`: 13,657 object / 2,033,633,420 B（約 2.0 GB） |
| minio_versioning | PASS | `unversioned`（最新版のみ複写される。履歴は無い） |
| **minio_kms** | **UNKNOWN** | コンテナに `MINIO_KMS_SECRET_KEY_FILE` が在る（名前のみ確認。値・ファイルは読んでいない）。サーバ側暗号化が有効なら、S3 GET で取る写しは**平文**でバックアップ先に置かれる。許容するかは所有者の判断（§5 R4） |
| credentials | PASS | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `POSTGRES_USER` は env-file にも環境にも**無く**、組み込み既定値で動いている（§5 R2） |
| free_space | PASS | DB root: 空き 1.87 TB / 必要 0.21 GB。object root: 空き 778 GB / 必要 4.07 GB（必要量 = 取得元 x 2） |
| **alembic** | **UNKNOWN** | 本番 DB は `0015`、このブランチの repo head は `0016`。0016（provider 予約の照合）の migration は**未適用**。deploy 中ではない（セッション 0）が、backup 取得と migration の順序を所有者が決める（§3 の順序 3） |
| schedule_window | PASS | 実行時刻が daily（06:00 JST = 21:00 UTC）・watchdog（毎時 :35）の窓に掛からない。時刻依存なので実行直前に再実行する |
| 見積り | — | 約 400 秒（DB 40 MB/s・object 30 MB/s・固定 60 秒の保守的な前提。初回の実測で置き換える） |

負荷: PG は 3 DB を順に `pg_dump`（各 1 つの REPEATABLE READ snapshot、全表に ACCESS SHARE。DML は止まらないが DDL/migration と競合する。
pgdata の NVMe を順次読む）。MinIO は全 object を順に GET（HDD の読み取りのみ、MinIO への書き込みなし）。書き込み量は最大でも約 2.1 GB
（2 つの root の合計）。

## 2. 手順表（所有者向け）

共通: 作業ユーザー `yoshiki`。env-file は `/home/yoshiki/projects/ai-video-platform-v2/.worktrees/claude-production-autopilot/.env`
（値は出さない）。コマンドは repo root から、venv の python で実行する。ロールバックは「新しい backup ディレクトリを消す」だけ
（所有者判断。本番の PG / MinIO / コンテナは変わらないので戻すものが無い）。

### 手順 0: 実行直前の precheck（`[R]`）

| 項目 | 内容 |
|---|---|
| 対象 | 本番 PG / MinIO / ホストのディスク（読み取り） |
| コマンド | `python scripts/backup.py --db-root /mnt/minio-hdd/avp-backups --object-root $HOME/avp-backups --env-file <env-file> precheck` |
| 所要・負荷 | 数秒。MinIO の list 1 回と数本の SELECT |
| 通す条件 | 判定が PASS。UNKNOWN は §1 の理由を所有者が承認済みなら可。FAIL は中止 |
| ロールバック | 不要 |

### 手順 1: 最初の backup（`[R]` + `[W]`）

| 項目 | 内容 |
|---|---|
| 対象 | 読む: `avp2-postgres-1` の 3 DB + globals、MinIO bucket `artifacts`。書く: 下記 2 つの root だけ |
| 書き込み先（正確なパス） | `/mnt/minio-hdd/avp-backups/<backup_id>/`（pg/*.dump, globals.sql, manifest.json）と `/home/yoshiki/avp-backups/<backup_id>/`（objects/, manifest.json）。`<backup_id>` は UTC の `YYYYMMDDTHHMMSSZ`。実行中は `.partial` |
| 空き容量 | DB root 1.87 TB / 必要 約 0.21 GB。object root 778 GB / 必要 約 4.07 GB |
| 必要な権限 | 現在のユーザーで足りる（root・sudo 不要）。`docker` グループ（rootless docker）。`/mnt/minio-hdd` の直下は `yoshiki:yoshiki 755` で root dir を作れる |
| コマンド | `export AVP_BACKUP_DB_ROOT=/mnt/minio-hdd/avp-backups AVP_BACKUP_OBJECT_ROOT=$HOME/avp-backups`<br>`python scripts/backup.py --env-file <env-file> run; echo $?` |
| 所要・負荷 | 見積り約 7 分（precheck の前提）。実測を記録する。NVMe と HDD の順次読み取り。daily（21:00 UTC）・watchdog（:35）と重ねない |
| リスク | `pg_dump` の snapshot が長く開く間 DDL が待たされる（migration を同時に走らせない）。HDD への読み取り負荷で MinIO の応答が遅くなりうる。KMS 有効なら平文の写しができる |
| 中止基準 | precheck が FAIL。daily/watchdog の窓の内側。実行中に migration/deploy が始まる。HDD/NVMe の空きが必要量を下回る。`run` が exit 1（自分の `.partial` を消し、audit.log と stderr に `backup.failed`） |
| 成功の判定 | exit 0、両 root に `<backup_id>/` が確定、`manifest.json` が `restore_verified: false`、`python scripts/backup.py status` が complete |
| ロールバック | 本番側は無変更。不要なら新しい `<backup_id>/` と root を所有者が削除する（唯一の後始末） |

### 手順 2: 隔離した復旧演習（`[D]`）— `restore_verified` を true にできる唯一の手順

| 項目 | 内容 |
|---|---|
| 対象 | 手順 1 の backup。復旧先は使い捨ての `avp-restore-*` コンテナ（`postgres:17-alpine`、ローカルに image 有り、`--network none`、ポート非公開）と作業ディレクトリ |
| 書き込み先 | `$HOME/.cache/avp-restore/avp-restore-<id>-<hex>/`（NVMe、backup 全体の複写で約 2.2 GB。終了時に消える）。本番の container / project / port / DB / bucket / `/mnt/minio-hdd/minio-data` / backup root と重なれば docker を呼ぶ前に exit 2 |
| 空き容量 | NVMe 725 GB / 必要 約 2.5 GB |
| 必要な権限 | 現在のユーザー（docker）。本番 PG には接続しない |
| コマンド | `python scripts/backup.py --env-file <env-file> restore-drill --backup-id latest --work-root "$HOME/.cache/avp-restore"; echo $?` |
| 所要・負荷 | 見積り 10 分前後（pg_restore と 2 GB の複写・再ハッシュ）。ローカルの NVMe と CPU を使う。本番は読まない |
| リスク | ローカルのディスク/CPU 負荷。演習用コンテナの後始末失敗（`restore.cleanup_failed`、exit 1 — 手で `docker rm -f avp-restore-*`） |
| 中止基準 | exit 2（拒否）は設定誤りの通報であり、緩めて再実行しない。exit 1 は失敗として扱い、原因を直して**新しい演習**をやり直す |
| **成功の判定** | exit 0 **かつ** 両 root の `manifest.json` が `restore_verified: true` で、`restore_drill` に 6 検査（全ファイル sha256・3 DB 復元・行数が backup 時の [before, after] に入る・alembic 一致・object sha256・DB が指す本体との一致）が全て pass の証跡（時刻・復旧先・sha256）が在る。`python scripts/backup.py verify --backup-id <id>` も 0 |
| ロールバック | 演習は自分の復旧先を消して終わる。残骸があれば所有者が `avp-restore-*` を消す |

### 手順 3: manifest を付けた preflight（`[R]`）

| 項目 | 内容 |
|---|---|
| 対象 | release preflight（読み取り専用）。`restore_verified` はまだ preflight の必須ではない（backup-restore.md §9。別の承認） |
| コマンド | `python scripts/release-preflight.py --target-sha <40hex> --backup-manifest /mnt/minio-hdd/avp-backups/<backup_id>/manifest.json --json` |
| 所要・負荷 | 数秒。本番へは読み取りだけ |
| 通す条件 | `backup_freshness` が stale/UNKNOWN でない（`db_dump` / `minio` / `temporal` の `created_at`）。手順 2 の `restore_verified: true` を目視で併せて確認する |
| ロールバック | 不要 |

## 3. 順序の制約

1. 手順 0 → 1 → 2 → 3 の順。**手順 2 が `restore_verified: true` になるまで、次へ進まない。**
2. **Temporal コンテナの再作成**（ADR-0052 / `docs/operations/temporal-container.md` §4）は、`restore_verified: true` **かつ**
   所有者の個別の承認の後でだけ。現在 `avp2-temporal-1` は unhealthy（zombie 17,934、pids 上限付近）だが、サーバ自体は動いており
   backup は取れる。**再作成を backup より先にしない**（失敗時に戻せる最新の検証済み集合が無くなる）。再作成中は 7233 が数十秒〜1 分止まる
   ため、daily（21:00 UTC）と watchdog の窓を避ける。
3. alembic `0016` の migration の適用は、backup（手順 1）の**後**で、手順 2 の検証済み集合がある状態で行う。backup 中に migration を走らせない
   （alembic revision が取得中に変わると `run` は失敗する）。
4. **Codex 設定の変更**と **fal の reconcile**（`reserved` の provider 予約の照合、ADR-0053）は、この 3 手順とも Temporal 再作成とも
   **別々の承認**。まとめて承認したことにしない。未照合の予約があるうちは再送しない（AGENTS.md §9）。
5. 定時実行（`deploy/backup/` の例）の install は、手順 1-3 の記録を見た後の別の承認。

## 4. 実行しなかったこと

- `run` / `restore-drill` / `prune --apply` / `verify`（root が無い）。`prune` は dry-run も走らせていない
- `/mnt/minio-hdd`・pgdata への書き込み、root の作成、コンテナの再起動・再作成
- Temporal / Schedule への接続（窓は `contracts/pipeline.py` の cron と `contracts/schedule_guard.py` の定数から計算した）
- ufw のルールの確認（root が要る。§5 R1）

## 5. read-only で見つかったリスク（修正はしていない）

| # | 観測 | リスク | 備考 |
|---|---|---|---|
| R1 | `0.0.0.0` と `[::]` に公開: PostgreSQL 5432 / MinIO 9000・9001 / Temporal 7233 / Temporal UI 8233 / API 8000（`docker ps` と `ss -ltn`）。ホストは `wlp3s0 192.168.3.19/24`（LAN）。`systemctl is-active ufw` は `active` だが、**ルールは root が要るため未確認** | LAN 上から DB・オブジェクト・Temporal・UI に届く可能性。rootless docker の公開は host の firewall の扱いが一般的な docker と異なる | 所有者が `127.0.0.1` 束縛（compose.yaml の `ports`）か firewall で閉じるかを決める。別の変更・別の承認 |
| R2 | `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` / `POSTGRES_PASSWORD` が env-file に無く、compose の既定値（`minioadmin` / `change-me` 系）で動いている。precheck の S3 list はその既定値で成功した | R1 と組むと、LAN から既定の認証情報で PG・MinIO に入れる | 値は出力していない。変更は MinIO/PG の再作成を伴うので別の承認 |
| R3 | `avp_test` が**本番 PG**に在る（`avp`, `temporal`, `temporal_visibility`, `postgres` と同居） | テストが本番 PG に接続できる配置。backup 対象外にしてあるが、負荷・事故の経路になりうる | INV の `TEST_DB_SUFFIX` ガードがあるが、物理的に分ける方が安全 |
| R4 | MinIO に `MINIO_KMS_SECRET_KEY_FILE`（サーバ側暗号化）。backup は S3 GET で復号された本体を保存する | バックアップ先（HDD の別 dir / NVMe の home）に**平文**の写しができる。鍵ファイルは backup 対象外なので、MinIO のデータ dir を直接丸ごと戻す方式は使えない（この設計は S3 経由の複写） | root/ファイルは 0700/0400。ディスク暗号化・offsite 暗号化は所有者の判断 |
| R5 | 2 つの backup は互いに**もう一方の本番ディスク**に載る（DB の写しは MinIO と同じ HDD、object の写しは pgdata と同じ NVMe）。offsite は未設定 | 1 台のディスク故障では片方しか失わないが、ホスト全体の喪失・誤削除・ランサムウェアには無力 | `AVP_BACKUP_OFFSITE_CMD`（backup-restore.md §7）は所有者が決める |
| R6 | `/mnt/minio-hdd/avp-db-backups/` に 9/19 の手作業 dump 2 本（各約 41 KB、0600）。`prune` は触らない | 手作業 dump は検証されておらず、今の 15 MB の `avp` とも粒度が違う。復旧手段として当てにしない | 所有者が残す/消すを決める |
| R7 | `docs/operations/temporal-container.md` は watchdog を「5分毎」と書くが、実装は `contracts/schedule_guard.py` の `35 * * * *`（毎時 :35） | 運用者が窓を取り違える | 別件として記録。この変更では直さない（AGENTS.md §3） |
| R8 | 本番 DB は alembic `0015`、このブランチは `0016` | 0016 を適用する deploy の前後で backup の世代が変わる | §3 の順序 3 |
| R9 | `avp2-temporal-1` は unhealthy のまま（ADR-0052） | pids 上限付近。バックアップ負荷とは無関係だが、再作成は検証済み backup の後 | §3 の順序 2 |

## 6. 変更が入ったコード（この preflight のために）

- `scripts/backup.py precheck`（`infrastructure/backup/precheck.py`、判定は `domain/backup/precheck.py`）。root が無くても作らず、audit も残さない。
  secret は名前だけを扱う。境界: `tests/architecture/test_backup_boundaries.py::test_precheck_is_read_only_against_production`
- `domain/backup/guards.py`: root と保護パスの重なりを `realpath` で判定し、`--allow-same-device` でも緩めない（独立レビューの指摘。
  `tests/unit/backup/test_guard_overlap.py`）。restore の `work_dir` 判定も `realpath` になった

## 補足: precheck の判定の読み方（独立レビュー反映）

- `credentials` は MinIO の資格情報が**組み込み既定値**のとき UNKNOWN。既定の宛先 `localhost:9000` が
  本番の `avp2-minio-1`（9000 を公開）であることを所有者が確認した上で承認する（開発機の別 MinIO を数えていないこと）。
- precheck の psql は `default_transaction_read_only=on` の接続で SELECT だけを送る。
- 承認トークン `OWNER-<yyyymmdd>-<id>` は `fullmatch`（末尾改行を許さない）。形式の確認であって認証ではない。
