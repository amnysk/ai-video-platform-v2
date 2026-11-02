# bgm-auto 検証（integration / オフライン / レビュー所見）のテストが存在する理由

| テスト | 止めるもの |
|---|---|
| `tests/integration/test_bgm_catalog_postgres.py::test_migration_0019_to_0021_to_0022_on_postgres` | 0021 の JSON 列が PostgreSQL で JSONB にならない（SQLite の unit では見えない）、0022 の CHECK 拡張を DB が受け入れない |
| `::test_0021_downgrade_refuses_while_catalog_rows_exist` | 行が残る監査表を downgrade が黙って DROP する |
| `::test_register_quarantined_duplicate_sha_makes_no_new_row` / `::test_concurrent_register_same_sha_yields_one_row` | 同じ sha256 の曲が二重に登録される（INV-133）。競合時に IntegrityError が漏れる |
| `::test_promote_then_list_entries_shows_approved_active` | promote が来歴・実測を消す、二度目の promote が承認を上書きする |
| `::test_concurrent_save_selection_once_one_row_same_decision` | 同時実行で選曲が後勝ちになり、Episode の決定が複数になる（INV-153） |
| `::test_finalize_selection_is_once_and_recent_usage_counts_finalized` | 確定の二重実行、未確定の選曲が直近使用回数に数えられる |
| `::test_quality_rates_is_read_only_against_postgres` | 集計 script が書く、READ ONLY 指定が PostgreSQL で効かない |
| `tests/unit/test_bgm_selection_offline.py` | 選曲・保存・確定の経路が（Commons 不通でなく）そもそもネットワーク接続を試みる |
| `tests/unit/test_bgm_review_findings.py`（strict xfail） | 実データの影の評価で見つかった所見: mood が合わない曲でも最低点を超える |
