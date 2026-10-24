# DB ロール分離の設計と適用手順（ADR-0055 Part 2）

**状態: 設計のみ。本番には未適用。** 各 SQL は staging（docs/operations/staging.md）で通してから、
所有者の明示的な判断で本番へ適用する。このリポジトリのコード・CI はこの手順に依存しない
（Part 1 のガードは単独で有効）。

## 現状（2026-10-24、読み取り専用で確認）

| 項目 | 値 |
|---|---|
| ロール | `avp` のみ。Superuser, Create role, Create DB, Replication, Bypass RLS |
| DB（全て owner = avp） | avp, avp_test, postgres, temporal, temporal_visibility, template0/1 |
| 接続者 | app / migrate / worker（`DATABASE_URL`）、Temporal auto-setup（`POSTGRES_USER`/`POSTGRES_PWD`）、テスト（`TEST_DATABASE_URL`）、backup |

**重要**: superuser は権限検査を全て迂回する。`avp` が superuser のままでは `REVOKE CREATE ON SCHEMA public FROM avp` は
**効かない**。DDL を実行時ロールから奪うには、アプリを別の非 superuser ロール（`avp_app`）へ移す必要がある。
`avp` は Temporal の bootstrap ロールとして残す（Temporal は自分の DB に DDL と CREATE DATABASE を要する）。

## 目標のロール

| ロール | 属性 | 用途 | 触れるDB |
|---|---|---|---|
| `avp` | 現状維持（superuser） | postgres イメージの bootstrap、Temporal auto-setup、緊急時の管理 | 全て |
| `avp_test_owner` | NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS | integration テストだけ | `avp_test` のみ |
| `avp_migrator` | 同上 | migrate サービス（Alembic）。`avp` のスキーマ所有者 | `avp` |
| `avp_app` | 同上 | api / worker の `DATABASE_URL`。DML のみ、DDL 不可 | `avp` |

## 手順

順序は A（追加のみ・無停止）→ B（テスト切替）→ C（アプリ切替）。各段で止められ、前段に依存しない。
実行は superuser `avp` で `psql -d postgres` 等から。**パスワードは SQL に書かず `\password` か secret 経由**
（INV-20）。

### A. テスト隔離ロール（追加のみ）

```sql
-- A1. ロールを作る（存在しても失敗しない形は psql の \gexec で）
CREATE ROLE avp_test_owner LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password avp_test_owner

-- A2. avp_test を作り直して所有者にする。REASSIGN OWNED は DB 自体の所有も動かすので使わない。
--     avp_test は使い捨て（ADR-0021）。中身を失ってよいことを確認してから:
DROP DATABASE avp_test;                       -- 対象は avp_test のみ。直前に \l で名前を確認
CREATE DATABASE avp_test OWNER avp_test_owner;

-- A3. 他DBへ入れなくする。既定では PUBLIC が CONNECT を持つので、PUBLIC から外す。
--     avp は superuser なので影響なし。Temporal は avp で繋ぐので影響なし。
REVOKE CONNECT ON DATABASE avp                 FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal            FROM PUBLIC;
REVOKE CONNECT ON DATABASE temporal_visibility FROM PUBLIC;
REVOKE CONNECT ON DATABASE postgres            FROM PUBLIC;
-- A4. 検証（avp_test_owner で）
--   psql -U avp_test_owner -d avp            -> FATAL: permission denied for database "avp"
--   psql -U avp_test_owner -d temporal       -> 同上
--   psql -U avp_test_owner -d avp_test -c 'CREATE TABLE t(); DROP TABLE t;'   -> OK
--   psql -U avp_test_owner -d avp_test -c 'CREATE DATABASE x'                  -> permission denied
```

**ロールバック A**:
```sql
GRANT CONNECT ON DATABASE avp, temporal, temporal_visibility, postgres TO PUBLIC;
ALTER DATABASE avp_test OWNER TO avp;
-- ロールを消すなら（avp_test_owner が何も所有しなくなってから）
DROP OWNED BY avp_test_owner;  DROP ROLE avp_test_owner;
```

### B. テスト・CI を `avp_test_owner` へ切替

`TEST_DATABASE_URL=postgresql+psycopg://avp_test_owner:…@localhost:5432/avp_test`。CI は
`deploy/postgres-init/` に A1〜A3 相当を足す（使い捨てなので `\password` ではなく環境変数経由）。
ローカルでは `TEST_DATABASE_URL` を差し替えるだけで戻せる（ロールバック = 元の URL）。
Part 1 のガードはこの段でも残す（二重の防壁）。

### C. アプリ実行ロールと migrator の分離（本番に影響する。maintenance pause 中に行う）

```sql
CREATE ROLE avp_migrator LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE avp_app      LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
\password avp_migrator
\password avp_app

\c avp
-- C1. スキーマ・全オブジェクトの所有を migrator へ（DB 自体の owner は avp のまま）
ALTER SCHEMA public OWNER TO avp_migrator;
DO $$ DECLARE r record; BEGIN
  FOR r IN SELECT format('ALTER TABLE %I.%I OWNER TO avp_migrator', schemaname, tablename) AS s
           FROM pg_tables WHERE schemaname = 'public'
  LOOP EXECUTE r.s; END LOOP;
  FOR r IN SELECT format('ALTER SEQUENCE %I.%I OWNER TO avp_migrator', sequence_schema, sequence_name) AS s
           FROM information_schema.sequences WHERE sequence_schema = 'public'
  LOOP EXECUTE r.s; END LOOP;
END $$;

-- C2. DDL を実行時ロールから奪う（PUBLIC と avp_app に CREATE を与えない）
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL    ON SCHEMA public FROM avp_app;
GRANT  USAGE  ON SCHEMA public TO avp_app;
GRANT  CONNECT ON DATABASE avp TO avp_app, avp_migrator;

-- C3. DML のみ
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO avp_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO avp_app;
-- 以後 migrator が作るオブジェクトにも自動で付く
ALTER DEFAULT PRIVILEGES FOR ROLE avp_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO avp_app;
ALTER DEFAULT PRIVILEGES FOR ROLE avp_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO avp_app;
```

その後 compose / `.env` で `DATABASE_URL` を `avp_app` に、`migrate` サービスだけ `avp_migrator` にする
（`compose.yaml` の `core-env` の `DATABASE_URL` と migrate の上書き。別 PR で ADR-0055 に従って行う）。
backup（scripts/backup.py）は読み取りのみなので、`GRANT pg_read_all_data TO <backup role>` か `avp` を使い続ける。

**検証**: `avp_app` で `CREATE TABLE t()` が `permission denied for schema public`、通常の API/worker の
smoke（scripts/smoke.sh）と `alembic upgrade head`（migrator）が通る。

**ロールバック C**（アプリを `avp` に戻せば即復旧。権限は広がるだけで壊れない）:
```sql
-- まず DATABASE_URL を avp に戻して再起動。その後（任意）
ALTER SCHEMA public OWNER TO avp;   -- 各テーブルも C1 と逆向きの DO ブロックで OWNER TO avp
GRANT CREATE ON SCHEMA public TO PUBLIC;  -- PG15 以降の既定は付かないので、元に戻す必要があるときだけ
```

## リスク

1. **Temporal を壊さない**: Temporal は `avp`（superuser）のまま。A3 の `REVOKE ... FROM PUBLIC` は superuser に影響しない。
   `temporal` / `temporal_visibility` の所有・ロールは触らない。Temporal 用 DB に `avp_app` を接続させない。
2. **`REASSIGN OWNED BY avp` は使わない**: DB 自体や Temporal の DB の所有まで動く。
3. **C の取りこぼし**: DDL を実行時に行うコードがあると本番で落ちる。実行コードに `create_all` が無いことは
   INV-69 の architecture test で担保済みだが、migration 以外の実行時 DDL（拡張・`CREATE INDEX CONCURRENTLY` 等）は
   staging で `alembic upgrade head` と smoke を通して確認する。
4. **シーケンス・将来のオブジェクト**: `ALTER DEFAULT PRIVILEGES` は **migrator が作る**オブジェクトにだけ効く。
   migration を `avp` で流すと `avp_app` に権限が付かない。migrate サービスを必ず `avp_migrator` にする。
5. **A2 の DROP DATABASE**: 対象名を取り違えると致命的。本番では実行直前に `\l` と `SELECT current_database()`、
   接続先が `postgres` DB であることを確認し、**手で `avp_test` を打つ**（コピペしない）。
6. **PG の同居は残る**: 同一インスタンスのまま。resource 枯渇（テストの重い負荷）は防げない。
   完全な隔離は別インスタンス（ADR-0055 Alternatives）。
7. パスワードは secret として管理し、ログ・コミットに出さない（INV-20）。
