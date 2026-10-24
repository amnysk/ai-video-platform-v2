# 破壊的スキーマ操作ガードのテストの理由（INV-69 / ADR-0055）

2026-09-15 の事故（integration テストの `drop_all` が本番 `avp` に走った）の再発を、URL の段階で止める。

## tests/unit/test_db_destructive_guard.py
- `test_protected_database_is_refused_before_any_connection`: 本番名・temporal*・postgres・大文字 `AVP`・`?options`・
  host 別名（127.0.0.1 / postgres）・リモート host の URL を `guarded_drop_all/create_all` に渡し、偽の接続の
  `execute` / `run_sync` が**呼ばれない**ことを見る。「拒否したが先に接続していた」を防ぐ fault injection。
- `test_engine_is_never_created_when_url_is_refused`: `TEST_DATABASE_URL` を本番に向けても `create_engine` が
  呼ばれずに収集時点で例外になる。
- `test_same_database_is_detected_through_url_variations`: アプリ DATABASE_URL と同じDBを、大文字 host・
  127.0.0.1・postgres・末尾ドット・query 付きの別表記でも検出する。表記を変えるだけで素通りする穴を塞ぐ。
- `test_different_port_or_database_is_not_the_same_database`: 逆方向。過剰拒否でテストが全部 skip/落ちするのを防ぐ。
- `test_case_difference_in_database_name_counts_as_the_same_identity`: 大文字小文字は保守的に同一扱い。
- `test_libpq_override_in_query_is_refused`: libpq は `?host=` / `?dbname=` で接続先を上書きする。名前検査を
  すり抜ける経路なので拒否する。
- `test_test_suffix_database_is_allowed`: `_test` は通る（ガードが何も通さなくなっていない）。
- `test_explicit_allowlist_...`: allowlist は `_test` 以外を通せるが、アプリDBと同一なら通さない。
- `test_empty_or_missing_url_is_refused`: 未設定を「安全」と解釈しない。
- `test_tests_support_reexports_the_single_guard`: 規則が2か所に複製されていないこと（AGENTS §8）。

既存の tests/unit/test_test_database_guard.py は変更していない（再公開後も同じ契約で通る）。

## tests/architecture/test_destructive_schema_ops_guarded.py
- 実行コードに `drop_all` / `create_all` / `DROP SCHEMA|DATABASE` が増えたら落ちる。tests は SQLite のみか
  ガード呼び出しのあるファイルだけ。変異（未ガードの呼び出しを足す）で赤になることを確認済み。

## tests/contract/test_ci_db_isolation.py
- ci.yml の `TEST_DATABASE_URL` が同じガードを通る（`_test`、DATABASE_URL と別DB）。integration の pytest step が
  必ず `TEST_DATABASE_URL` を持つ。CI が本番形の DB へ破壊的テストを向ける変更を PR で止める。
