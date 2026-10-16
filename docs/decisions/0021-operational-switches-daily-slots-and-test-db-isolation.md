# ADR-0021: 運用スイッチ・日次 Episode 枠と、テストDBの隔離

## Status

Accepted (2026-09-15)

## Context

自動運転（Temporal Schedule から毎日 Episode を1本起こす）に向けて、次の3つが足りない。

- **停止スイッチ**: failure-policy §7 の `PAUSED` / `UPLOADS_PAUSED` は環境変数だけで、止めるには worker の再起動が要る。
  走っている Schedule / Activity を再デプロイなしで止める手段が無い
- **1日の本数上限**: Schedule の再試行・手動 trigger・Activity の再実行が重なると、同じ日に Episode が
  複数作られうる。Episode 作成は無料でも、その先の production は有料 provider を呼ぶ（INV-15）
- **テストが開発DBを消す**: `tests/integration/test_postgres_repositories.py` は `DATABASE_URL`（開発DB `avp`）に
  対して `drop_all` を実行していた。`test_migration_against_postgres.py` も同じ URL から `DROP DATABASE` を発行する。
  開発スタックを起動したまま integration テストを流すと、開発データが消える
- 加えて Alembic の `env.py` が `fileConfig` を既定の `disable_existing_loggers=True` で呼んでおり、
  テストから upgrade すると import 済みロガーが無効化され、caplog に頼る3テストが実行順で落ちていた

## Decision

**停止スイッチと日次枠を PostgreSQL の2テーブルに置き、上限は主キーで守る。
破壊的な integration テストは `TEST_DATABASE_URL`（`_test` で終わるローカルDB）にだけ向け、条件外は例外で止める。**

### 1. 語彙（唯一の宣言元 `contracts/operations.py`）

| 名前 | 値 |
|---|---|
| `OperationalSwitch.PAUSED` | `paused` |
| `OperationalSwitch.UPLOADS_PAUSED` | `uploads_paused` |
| `ClaimOutcome` | `created` / `existing` / `resume` / `limit_reached` |

### 2. テーブル（migration 0007）

- `operational_switches(name PK CHECK 語彙, is_on, reason, updated_at)`。**行が無ければ off**。
  実効値は環境変数との OR（環境変数で止めたものを DB で解除はできない）
- `daily_episode_slots(slot_date, slot_index, trigger_id UNIQUE, episode_id UNIQUE FK episodes, created_at)`、
  PK `(slot_date, slot_index)`、`slot_index >= 0`

### 3. claim の意味論（`DailyEpisodeSlotRepository.claim`）

1. 同じ `trigger_id` の枠があれば `EXISTING`（Activity 再試行は冪等）
2. その日の枠数 ≥ 上限なら、未着手（`planned`）の Episode を持つ枠を `RESUME`、無ければ `LIMIT_REACHED`
3. それ以外は savepoint 内で Episode（`planned`）と枠を作って `CREATED`。`IntegrityError`（同じ番号・同じ trigger を
   別の claim が先に取った）は savepoint を戻して 1 から読み直す

並行性の保証は**アプリの読みではなく主キー**に置く。read committed で2つの claim が同時に「0本」を読んでも、
同じ `slot_index` の2本目は挿入できない。前日の `blocked` Episode は当日の枠数に数えない（日付で分かれる）。
Episode 状態機械は変更しない。

### 4. テストDBの隔離（`tests/support/db.py`）

- integration テストは `TEST_DATABASE_URL` **だけ**を読む。未設定なら skip。`DATABASE_URL` へは落ちない
- DB名は `_test` で終わる、ホストは `localhost` / `127.0.0.1` / `::1` / `postgres`（それ以外は
  `AVP_ALLOW_REMOTE_TEST_DB=1`）、`DATABASE_URL` / `Settings().database_url` と同じDBでない。
  満たさなければ `UnsafeTestDatabaseError`（収集時点のエラー。skip にしない）
- `create_all` / `DROP SCHEMA` / `DROP DATABASE` の直前に `assert_destructive_allowed` を再度呼ぶ
- 各テストは一時スキーマに閉じ込める。Alembic の probe DB は `avp_alembic_probe_test`
- `avp_test` は `deploy/postgres-init/01-test-db.sql` が初回起動時に作る。CI も同じ

### 5. Alembic のロギング

`env.py` は `config.attributes["configure_logger"]`（既定 True）のときだけ `fileConfig` し、
`disable_existing_loggers=False` を渡す。テストは `False` を設定する。

## Alternatives

- **(a) 上限を「その日の episodes を数えて作る」だけで守る**: 並行 claim で2本できる。
  `SELECT ... FOR UPDATE` は数える対象の行が無いと何もロックしない。advisory lock は SQLite で試験できない。採らない
- **(b) スイッチを Temporal の Schedule pause だけにする**: 実行中の Activity や upload を止められず、
  API から状態を読むにも Temporal を引く必要がある（INV-8: Temporal は状態の権威ではない）。採らない
- **(c) 枠の列を episodes に足す**: 手動 POST の Episode と自動生成が混ざり、NULL を含む一意性の方言差が出る。採らない
- **(d) テストDBを `DATABASE_URL` のまま一時スキーマだけで守る**: `drop_all` のような1行の書き漏れで開発データが消える。
  名前規則とホスト検査で「そもそも向かない」ようにする方が堅い。採らない
- **(e) `TEST_DATABASE_URL` 不正時に skip**: 誤設定が黙ってテスト未実行になる。例外にする

## Consequences

- 良い: 再デプロイなしで停止できる。1日の本数上限が並行でも破れない（実PostgreSQL の並行テストで検査）
- 良い: 開発DBに対する integration テストの実行は収集時に拒否される
- 悪い: `RESUME` は並行時にも返るので、同じ Episode の pipeline を2つの起動が同時に始めうる。
  子 workflow id を Episode から決定的に作り「already started」を成功扱いにすることで吸収する前提（負債）
- 悪い: 既存の開発ボリュームには init SQL が走らない。`avp_test` は手で作る必要がある
- 悪い: 環境変数と DB の2系統のスイッチを運用者が意識する必要がある（OR で合成する）
- 悪い: downgrade は2テーブルを行ごと消す（運用状態であり Episode は残る）
