# 破壊的 DB 操作ガードのテストの理由（INV-69 / INV-83 / ADR-0055）

2026-09-15 の事故（integration テストの `drop_all` が本番 `avp` に走った）の再発を止める。URL の静的検査（INV-69）は
URL に現れない上書き（`connect_args` / `creator` / libpq 環境変数）を見られないので、実接続の動的検査（INV-83）を足した。
各テストは「拒否した」だけでなく **DDL が走っていない** ことを断言する（拒否のつもりで実行していた、を防ぐ）。

## tests/unit/test_db_destructive_guard.py
静的検査
- `test_protected_database_is_refused_before_any_connection`: 本番名・temporal*・大文字・`?options`・host 別名・リモート
  を `guarded_*` に渡し、偽の接続に **一度も SQL が流れない** こと（静的検査が接続より前）と DDL が呼ばれないこと。
- `test_a_url_that_does_not_fix_the_target_is_refused`: host 無し・DB 名無しの URL（libpq の `PGHOST` / `PGDATABASE`
  任せ）を localhost 扱いしない。
- `test_libpq_environment_that_can_redirect_the_connection_is_refused`: `PGHOSTADDR` / `PGSERVICE` / `PGSERVICEFILE` が
  URL の明示を超えて接続先を変えうる。実プロセスの環境を既定で読むことも別のテストで固定。
- `test_pghost_does_not_matter_when_the_url_names_the_host`: 逆方向。URL が host を明示していれば `PGHOST` は無関係
  （過剰拒否で全 integration が止まらない）。
- `test_only_loopback_names_are_local`: compose のサービス名 `postgres`・`0.0.0.0`・任意名・外部 IP を接続先として許さない
  （`AVP_ALLOW_REMOTE_TEST_DB` の明示でのみ通る）。`test_loopback_test_database_is_accepted` は許す側。
- `test_same_database_is_detected_through_url_variations`: 本番 URL が `postgres` や `0.0.0.0` でも、`localhost` の同じ DB を
  同一と見なす（許可リストから外しても同一性の判定からは外さない）。逆方向は `test_different_port_or_database_...`。
- `test_libpq_override_in_query_is_refused` / `test_explicit_allowlist_...` / `test_empty_or_missing_url_is_refused`。

動的検査（実接続）
- `test_live_check_accepts_only_a_marked_test_database` と `test_live_check_refuses_everything_else`: 本番名・印無し・
  別の印・URL と別の `_test` DB・空名・大文字名を、純粋関数として網羅。印を外した実装でも `_test` 名だけでは通らない。
- `test_a_connect_args_style_override_is_stopped_by_the_live_check`: URL は `avp_test`（静的検査は通る）だが実際は
  `avp` に繋がっている偽の接続。`drop_all` / `create_all` が呼ばれないこと。
- `test_a_marked_test_database_runs_the_ddl`: 印のある DB では DDL が走る（何も通さない実装との区別）。
- `test_an_unmarked_test_database_is_refused_even_with_a_clean_url`: 本番インスタンス上の `avp_test` で印が無い場合。
- `test_drop_database_checks_the_target_and_the_connected_database`: 消す対象の名前の検査と、名前に仕込んだ
  `; DROP DATABASE` の注入。

文の入口
- `test_destructive_statements_are_verified_before_they_run`: `DROP TABLE|SCHEMA|DATABASE` / `TRUNCATE`、小文字、
  先頭の空白・コメント、`;` 連結の2文目でも、実行前に接続の検証が呼ばれる。
  追記（独立レビュー E の P2-4）: `/* ; */ DROP ..`・入れ子コメント・`'..;..'` と `$$ ; $$` の後ろの DROP・
  `DO` + `EXECUTE`・`DO` 内の DROP・`ALTER TABLE .. DROP [COLUMN]`・`WHERE` 無し `DELETE`（コメント内の `WHERE` は無効）を
  加えた。以前は `;` で分割してからコメントを剥がしていたので、コメント内の `;` で DROP が見えなくなっていた。
- `test_other_statements_are_not_touched`: `SELECT` / `INSERT` / `DELETE ... WHERE` / `ALTER` / 文字列中の `DROP TABLE` に
  過剰反応しない（通常のテストが壊れない）。追記: `DROP CONSTRAINT` / `DROP DEFAULT|NOT NULL`、文字列・コメント中の
  `;` や `DROP TABLE`、`$$DROP TABLE x$$`、`DO` で `PERFORM` だけのもの、サブクエリ付きの `DELETE ... WHERE` も触らない。
- `test_destructive_detector_scope_is_documented_in_one_place`: 公開関数 `is_destructive_statement` は既定で
  `WHERE` 無し `DELETE` を数えず（architecture テストの拾うリテラルに過剰反応しない）、実行時の守衛だけが数える。
  integration への影響: `tests/integration/test_migration_against_postgres.py` などの `DELETE FROM episodes`（WHERE 無し）は、
  印のある `avp_test` では接続ごとに 1 回の live check を通るだけで通る。印の無い DB では拒否される（意図どおり）。
- `test_drop_database_names_must_end_with_test_even_on_a_verified_connection`: 検証済みの接続からでも、`avp` /
  `temporal` を消す文は拒否。

削除したテスト（理由: 何も証明していなかった）: `test_engine_is_never_created_when_url_is_refused`（ガードは
`create_engine` を呼ばないので、差し替えても常に通る）、`test_tests_support_reexports_the_single_guard`
（同一オブジェクトの確認。`test_support_db_module_only_reexports_the_guard` が「定義を持たない」ことを見る）、
`test_case_difference_...`（`test_same_database_is_detected_...` に包含）。

既存の `tests/unit/test_test_database_guard.py::test_local_test_database_is_accepted` は、`postgres` を許可リストから
外す仕様変更（ADR-0055 追補）に合わせて、許す host から `postgres` を外した。他は変えていない。

## tests/architecture/test_destructive_schema_ops_guarded.py
AST で **箇所ごと** に検査する。ファイルに1か所ガードがあれば別の関数の `DROP TABLE` が通る、という旧実装の穴
（正規表現のファイル単位）を塞ぐ。
- `test_every_destructive_call_site_is_guarded`: 実コード（実行コード・tests・scripts）の全箇所。
- `test_checker_flags_only_the_unguarded_function_in_a_file_with_a_guard`: 同じファイルの隣の関数だけが落ちる。
- `test_checker_requires_the_guard_before_the_operation`: ガードが後ろにあっても通さない。
- `test_checker_covers_every_destructive_form`: `drop_all` / `create_all`（参照も）/ `drop_table` / `TRUNCATE` /
  `DROP SCHEMA`（f 文字列）/ `DROP DATABASE` / 小文字。
- `test_checker_ignores_docstrings_comments_and_non_sql_strings`: 誤検出で検査自体が無視されるのを防ぐ。
- `test_sqlite_exemption_is_per_function_and_tests_only`: SQLite の免除は関数単位・tests のみ・`postgresql` が混ざれば無効。
- `test_support_db_module_only_reexports_the_guard`: 規則の複製が無い（AGENTS §8）。

## tests/integration/test_destructive_db_mistake.py（失敗注入12）
実 PostgreSQL に本番に見立てた DB（表と3行、`_test` で終わらず、印なし）・印の無い `_test` DB・印のある `_test` DB を作り、
URL・`connect_args`・`creator`・libpq 環境変数・`DROP DATABASE` の経路で誤って破壊的操作をしても、DDL が走る前に
`UnsafeDatabaseError` になり、**実 DB の行数が変わらない** ことを生の接続で確認する。対照テスト（印のある DB は通る）が
あるので、ガードが何も通さない実装・何も止めない実装のどちらでも赤になる。ガードの動的検査を無効化する変異で
4 件が赤になることを確認済み。

## tests/contract/test_ci_db_isolation.py
ci.yml の `TEST_DATABASE_URL` が同じ静的検査を通ること、integration / alembic の pytest step が必ず持つこと、
`TEST_DATABASE_URL` を持つ step は `AVP_MARK_TEST_DB=1` も持つこと（付け忘れると integration が全滅する）。
