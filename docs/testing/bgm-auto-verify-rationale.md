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

## 独立レビューの修正（統合枝、2026-11-02）

- `test_bgm_review_findings.py::test_a_track_whose_mood_does_not_match_is_never_suitable_even_at_the_minimum_score`:
  F1（P1）。実データの影の評価で、tense な戦の Episode に calm の曲が最低点ちょうど（400）で選ばれた。mood 以外の項目だけで
  525 点まで届くので、点の閾値では「合わない曲しか無い」を判定できない。strict xfail で所見を示していたものを、修正
  （`is_suitable` = 最低点 + mood 一致、規則 select-2）の回帰検査に置き換えた。単体の段階なのは純関数の規則だから。
- `test_render_activities_bgm_auto.py::test_a_finalized_decision_is_rechecked_before_it_is_reused`: F3。確定済みの決定を再利用
  する経路だけが描画直前の門（INV-97 + INV-151 の規則の版）を通っていなかった。確定後に失効した曲が再試行で鳴らないことを、
  旧コードで落ちることを確かめてから固定した。
- `test_bgm_catalog_import.py::test_the_evidence_hash_is_computed_from_a_real_file_never_typed_in`: F6。所有者の承認が手で打った
  任意の 64 桁 hex で通っていた。証拠のバイト列から計算し、無い・空のファイルでは承認しない。
- F2（energy が常に high）は `requirements._energy` の cut 数の境を実データ（Shorts は 12〜17 cut/分）に合わせて直し、
  既存の要件のテストが引き続き通ることで確かめた（cut 数だけで high にならない）。
