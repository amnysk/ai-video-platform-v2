# ADR-0055: 破壊的スキーマ操作の単一ガードと DB ロール分離

## Status

Proposed (2026-10-24)。Part 1（ガード）は実装済み。Part 2（ロール分離）は**設計のみ**で、本番には未適用。

## Context

2026-09-15、`test_artifact_generation_a_b_a_on_postgres` が `DATABASE_URL` を直接使い、
`drop_all` / `create_all` が本番DB `avp` に走り、fixture 行が2件残った（2b48409 が
`TEST_DATABASE_URL` と `_test` 名ガードで塞いだ。ADR-0021）。残る弱点:

1. 守りが tests/support/db.py の URL 名検査だけで、`drop_all` / `create_all` を呼ぶ他のコードには無い。
2. 本番 `avp` と `avp_test` が同じ PG インスタンス・同じ superuser `avp` を使う。名前を間違えれば何でもできる。

読み取り専用で確認した現状（`docker exec avp2-postgres-1 psql`）: ロールは `avp` ただ1つで
Superuser / Create role / Create DB / Replication / Bypass RLS。DB は avp, avp_test, postgres,
temporal, temporal_visibility（全て owner = avp）。

## Decision

### Part 1: 単一ガード（実装済み）

`infrastructure/db/safety.py` が唯一の判定元（AGENTS §8）。`assert_destructive_allowed(url)` と
`guarded_drop_all` / `guarded_create_all` は、DB名が `_test` で終わる（または明示 allowlist）ときだけ通し、
**接続を作る前に URL だけで**拒否する。同一DB判定は host 別名・末尾ドット・DB名の大文字小文字・query を畳む。
`?host=` / `?dbname=` など接続先を上書きする query は拒否する。tests/support/db.py は再公開のみ。
実行コードは safety.py の外で `drop_all` / `create_all` / `DROP SCHEMA|DATABASE` を呼べず、tests は
SQLite のインメモリかガード呼び出しのあるファイルに限る（architecture test）。INV-69。

grep 監査（実測、apps/ workers/ domain/ infrastructure/ scripts/ contracts/ tests/）:
- 実行コードの `drop_all` / `create_all` / `DROP SCHEMA|DATABASE`: 0 件（`infrastructure/db/models.py` のコメント1件は無関係）。
- tests の `create_all` on SQLite インメモリ（読み手: 無害）: conftest.py と unit 5 ファイル。
- tests の PostgreSQL 破壊的操作（`create_all` + `DROP SCHEMA`/`DROP DATABASE`）: integration 約 20 ファイル、
  全て `assert_destructive_allowed` 経由。
- `TRUNCATE` / `DROP` の文字列: 検査用の正規表現・migration 走査の fixture のみで、実行されない。

CI: integration / alembic job は使い捨ての compose / service container の PostgreSQL に `avp_test` を作り、
`TEST_DATABASE_URL` は `_test` のみ（job 終了時に `down -v`）。本番に繋がる経路は無い。
`tests/contract/test_ci_db_isolation.py` が ci.yml の URL を同じガードで検査する。CI の変更は不要だった。

### Part 2: ロール分離（設計のみ。手順は docs/operations/db-role-separation.md）

- `avp_test_owner`（NOSUPERUSER NOCREATEDB、`avp_test` だけ所有、他DBへ CONNECT 不可）を作り、テストはこれで接続する。
  PUBLIC の CONNECT を avp / temporal* / postgres から外す。
- アプリ実行ロール `avp_app`（DML のみ、DDL 不可）と、migrate 専用の `avp_migrator`（スキーマ所有者）を分ける。
- **`avp` は superuser のまま Temporal auto-setup 専用の bootstrap ロールとして残す**。superuser は権限検査を
  すべて迂回するので、`avp` から `REVOKE CREATE` しても効かない。アプリ側を別ロールへ移すのが唯一の有効な手段。

## Alternatives

- `avp` を NOSUPERUSER にする: Temporal auto-setup が `CREATE DATABASE` / 拡張を要し、postgres イメージの
  bootstrap も壊れる。不採用。
- 別 PG インスタンスにテストDBを置く: 最も強いがメモリ・ポート・compose の追加が要る。Part 2 の次段として残す。
- ガードを env で無効化できるようにする: 事故の原型。不採用（allowlist は呼び出しごとの引数でのみ渡す）。

## Consequences

- 新しい破壊的テストは `assert_destructive_allowed` を呼ばないと architecture test で落ちる。
- Part 2 を適用するまで、`avp` superuser が avp_test と本番の両方に繋がる事実は変わらない（URL ガードだけが防壁）。
