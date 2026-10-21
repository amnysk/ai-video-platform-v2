# ADR-0051: 復旧できるバックアップ（pg_dump + S3 API 複写）と、隔離先への restore drill

## Status

Proposed (2026-10-21)。所有者の承認で Accepted にする。承認までは本番で実行しない。

## Context

`docs/operations/release-runbook.md` §0 は「バックアップを取る自動手順はこの repo に無い」と書いている。
ADR-0050 の rollback も、contract migration が流れた後は「バックアップからの復旧」に頼る。
しかし現状のバックアップは `/mnt/minio-hdd/avp-db-backups` の 2 ファイル（2026-09-19、各 41 KB。
`avp` だけ・手作業）だけで、**復旧できると確かめた記録が無い**。実測（2026-10-21、読み取りのみ）:

| 対象 | 場所 | 実測 |
|---|---|---|
| PostgreSQL `avp`（アプリ） | コンテナ `avp2-postgres-1`、volume `avp2_pgdata`（host の NVMe `/`） | 14 MB、alembic `0015` |
| PostgreSQL `temporal` / `temporal_visibility` | **同じ** PG（compose の `DB: postgres12`、`POSTGRES_SEEDS: postgres`。`\l` で確認） | 44 MB / 43 MB |
| `avp_test`, `postgres`, `template*` | 同じ PG | テスト用 26 MB 等。対象外 |
| MinIO `artifacts`（本番 bucket） | bind `/mnt/minio-hdd/minio-data`（別ディスク `sda1`、ext4） | 約 2.1 GB。DB の `artifact_metadata` 592 行が参照 |
| MinIO `artifacts-test` / `artifacts-itest` | 同上 | 186 MB / 136 KB。テスト bucket、既定では対象外 |
| 作業領域 | `/mnt/minio-hdd/ai-video-work` | 372 KB。**一時領域で正式 Artifact ではない**（work-directories.md）ので対象外 |

要するに全体で数 GB・単一ホスト。Temporal の状態は PG の中にあり、MinIO の本体は不変（INV-11）で
アプリは `remove_object` を呼ばない（grep 済み）。

守るべき失敗: (1) NVMe の故障で `avp2_pgdata` を失う (2) HDD の故障で MinIO を失う
(3) 誤った migration / 誤操作で DB を壊す (4) 「バックアップはあるが戻せない」(5) 復旧作業そのものが
本番を壊す（本番へ restore してしまう）。

## Decision

**PostgreSQL は `pg_dump --format=custom`（DB ごと）+ `pg_dumpall --globals-only`、MinIO は S3 API
（既存依存の `minio` SDK）による複写、を 1 つの backup 集合に束ね、manifest を `restore_verified: false` で
作る。それを「隔離した復旧先へ戻して検証する restore drill」だけが `true` にできる。**

### 1. 道具の比較と選択

PostgreSQL:

| 候補 | 評価 |
|---|---|
| **pg_dump -Fc（採用）** | コンテナ内の同版バイナリを `docker exec` で使う。本番コンテナ・設定を変えない。DB ごとに一貫したスナップショット。論理 dump なので版を跨いで戻せる。RPO は取得間隔（日次）。PITR は無い |
| pgBackRest / WAL-G | PITR・差分に優れるが、`archive_mode=on` は PG の**再起動**（本番コンテナの再作成）が要り、postgres:17-alpine にバイナリも無い。100 MB 規模には過剰で、導入作業が本番の変更になる（AGENTS.md §9）。DB が数十 GB になる／RPO を日次より短くする必要が出たら再検討（下の Consequences） |
| ファイルシステム複写（restic で pgdata） | 稼働中の pgdata は一貫しない。止めるなら停止が要る。採らない |
| `pg_basebackup` | 再起動は要らないがレプリケーション設定とバイナリ版の管理が増え、得るものが無い |

MinIO（オブジェクト）:

| 候補 | 評価 |
|---|---|
| **S3 API 複写（採用）** | 新しい依存が無い（`minio` は `pyproject.toml` に宣言済み。AGENTS.md §8）。list → get で稼働中でも一貫した個別オブジェクトを得る。sha256 を取りつつ etag（md5）で転送を検査。不変なので前回と同じ key/size/etag は hardlink で共有（増分） |
| `mc mirror` | 同等だが `mc` が host に無い（コンテナ内にはある。本番コンテナへ `exec` したくない）。新しい配布物の管理が増える |
| rclone | 同上。設定ファイルに資格情報を置く運用が増える |
| restic | 暗号化・重複排除・世代管理に優れる。**オフサイト用に推奨**（下の 4）。ローカルの第一の写しには、restic の repository が不透明で復旧に restic が要ること、manifest の sha256 と DB 行の参照を直接突き合わせたいことから採らない |
| `/mnt/minio-hdd/minio-data` の直接複写 | MinIO の内部形式（`xl.meta`、小物はインライン）で、書き込み中の複写は一貫しない。採らない |

### 2. 一貫性の境界（PG ↔ MinIO）

- 順序は **PostgreSQL（3 DB）→ MinIO**。Artifact は不変で、DB の行は本体の put の**後**に commit される。
  だから dump に載る行の参照先は、後から取る MinIO の写しに存在する（逆順では保証できない）。
  MinIO にだけ在る本体は孤児で害が無い。両方の開始・終了時刻を `manifest.consistency` に記録する
- 各 DB の行数は dump の前後に数え `rows.<table> = {before, after}` を残す。復旧後の行数が
  `[before, after]` に入らなければ drill は失敗する（書き込み中の取得を許しつつ、欠落は検出する）
- alembic revision を取得の前後で比べ、**変わっていたら失敗**にする（migration 中の dump は採らない）
- 静止（quiesce）は既定で行わない。必要なら `scripts/with-maintenance-pause.sh` で包めるが、
  そのスクリプトは失敗時に pause を**解除しない**ため、無人の定時実行には使わない（runbook 参照）。
  manifest の `consistency.quiesce` は `none` か、運用者が申告した値で、証明ではない
- Temporal は PG の `temporal` / `temporal_visibility`。アプリ DB と別スナップショットなので、復旧後は
  workflow の履歴がアプリ DB と食い違いうる。権威はアプリ DB（INV-7 / INV-8）。復旧後に
  `reserved` のまま残る provider 予約は**再送しない**（INV-15 / ADR-0013）

### 3. 保存先（故障単位を分ける）

保存先は **2 つの root**（`AVP_BACKUP_DB_ROOT` / `AVP_BACKUP_OBJECT_ROOT`）で、環境変数が無ければ実行しない
（既定の保存先は無い）。各 root が守る対象と**同じデバイスなら拒否**する（`--allow-same-device` で
明示した場合だけ許し、manifest に `same_device_as_source: true` を残す）。この host は NVMe（pgdata）と
HDD（MinIO）の 2 台だけなので、DB の写しは HDD に、オブジェクトの写しは NVMe に置く（互いに相手の故障から守る）。
manifest は両 root に同じ内容を置く。

これは**ホスト全体の喪失**（火災・盗難・ランサム・誤った `rm -rf`）を防がない。そのためのオフサイトは
**任意で既定オフ**: `AVP_BACKUP_OFFSITE_CMD` に argv（`{db_set}` `{object_set}` を置換）を設定したときだけ、
検証前の集合を送る。何も設定しなければ外部送信は一切しない。推奨は restic（暗号化）を外付けディスク
または所有者が選ぶ保存先へ。送信先・資格情報の選定は所有者の判断。

### 4. manifest（release preflight と互換）

`components.{db_dump,minio,temporal}.created_at`（TZ 付き ISO8601）を `domain/release_preflight.py` の
`BACKUP_COMPONENTS` と同じ名前で出す（`tests/contract/test_backup_contracts.py` が preflight の
`_check_backup` を通して PASS になることを検査）。追加の欄: `schema_version`、`backup_id`、`started_at` /
`finished_at`、`git.{revision,dirty}`、`alembic_revision`、`tool_versions`、`databases.<db>.{file,sha256,bytes,rows}`、
`objects.{count,bytes,buckets}`、`files.<相対パス>.{sha256,bytes,etag,root}`（**全ファイル**）、
`consistency`、`destination`、`offsite`、`restore_verified`、`restore_drill`。
**preflight 側の判定は変えない**（`restore_verified` を必須にするかは別の変更・別の承認。Consequences）。

### 5. restore_verified と restore drill

- backup は常に `restore_verified: false` で確定する。`true` にできるのは drill だけで、
  `domain.backup.manifest.apply_drill`（全検査 pass の証跡があるときだけ）を通る。証跡は manifest の
  `restore_drill` に残す（時刻・復旧先・各検査・sha256）
- drill は **隔離した復旧先にだけ**書く: 使い捨ての `postgres:17-alpine` コンテナ（名前 `avp-restore-*`、
  loopback の一時ポート、DB 名 `restore_*`）と、使い捨ての作業ディレクトリ。復旧先の
  container / compose project / host / port / DB 名 / bucket 名 / 作業パスのどれかが本番と一致したら、
  何も実行せずに拒否する（`domain/backup/guards.py`）
- 検査: (1) 全ファイルの sha256 が manifest と一致 (2) 3 DB が戻る (3) 行数が `[before, after]` に入る
  (4) alembic revision が manifest と一致 (5) オブジェクトの sha256 (6) DB の `artifact_metadata` /
  `research_artifacts` の全行について、(bucket, object_key) の本体が存在し sha256 が行と一致
- 保持（日 7 / 週 4 / 月 6）の pruner は、**未検証・未完了の集合を決して消さず、最新の検証済み 1 つも消さない**

### 6. 失敗と通知

失敗は非ゼロ終了（1）+ stderr / `audit.log` への JSON 1 行（`backup.failed`）+ 任意の通知
（`AVP_BACKUP_NOTIFY_CMD` / `AVP_BACKUP_NOTIFY_WEBHOOK`、既定オフ。通知の失敗は結果を変えない）。
失敗した取得の `*.partial` は自分で消し、確定した集合は作らない。

新しい不変条件: INV-66。定時実行の unit は `deploy/` に**例として**置くだけで、install はしない。

## Alternatives

- **pgBackRest / WAL-G を今入れる**: 採らない（上の表）。本番の PG を再起動してから初めて価値が出る
- **restic で全部**: 採らない。オフサイトには使う。第一の写しは、復旧に別ツールを要さず manifest と直接突き合わせられる形にする
- **drill を本番の別 DB / 別 bucket へ戻す**: 採らない。同じ PG・MinIO 上で名前だけ違う復旧は、1 つの設定誤りで本番に触れる。コンテナごと別にする
- **MinIO のバージョニングで代替**: 採らない。同じディスクの上で、DB との対応も取れない
- **quiesce を既定にする**: 採らない（無人の失敗が Daily を止める。上の 2）

## Consequences

- 良い: 「戻せる」が証跡つきで言える。復旧先が本番になる事故が機械的に拒否される。数 GB なので復旧演習は数分
- 悪い（負債）:
  - RPO は取得間隔（日次推奨）。PITR は無い。DB が肥大するか日次より短い RPO が必要になったら pgBackRest を別の ADR で
  - オフサイトが未設定のあいだ、ホスト全体の喪失には無力
  - 前回と同じ不変オブジェクトは hardlink で共有するため、媒体の bit-rot は全世代に及ぶ。
    `scripts/backup.py verify` で定期的に全ファイルを再ハッシュする
  - 取得は MinIO の HDD を数分読む（render / production worker と I/O が競合しうる）。`pg_dump` は
    ACCESS SHARE のみで書き込みを止めないが、DDL（migration）とは競合するので deploy 中は走らせない
  - `globals.sql` はロールのパスワードハッシュを含む。0600 のファイルで、オフサイトへ送るなら暗号化が要る
  - drill は MinIO サーバーを起動しない（オブジェクトは作業ディレクトリのファイルとして検証）。
    MinIO のデータ形式から戻すのではなく、API から取った本体を元に戻す設計である
- 次の変更（本 ADR の範囲外・別承認）: release preflight に「直近の backup が `restore_verified` であること」を
  足す（`backup_freshness` の追加条件）。足すまでは、検証されていない backup でも preflight は PASS しうる
