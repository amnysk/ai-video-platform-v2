# 判断記録と介入: 既存接続・ログ・試験（担当 D）のテストの理由

JP-MAS-001 v2.0 / ADR-0041。各テストが**何の事故・契約**を守り、**なぜその層**で書き、**何の失敗を捕まえる**かを
記録する（AGENTS.md・memory「test-design-rationale」）。fake / stub を使うものはそう明記する。

## 1. ログ語彙（`tests/contract/test_intervention_log_vocabulary.py`）

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_every_intervention_event_is_a_log_event_name` | ADR-0041 §7: `episode_events.event_name` とログの `event_name` は同じ文字列 | 2つの enum の集合比較だけで決まる。実行は要らない | `InterventionEvent` にだけ足して投影がログ語彙に無い値を出す（AGENTS.md §8 の片側更新） |
| `test_intervention_event_members_have_the_same_python_names` | 投影の対応表をメンバー名で引ける | 同上 | 文字列は同じでもメンバー名がずれ、対応表の網羅検査が通らない |
| `test_correlation_fields_of_the_design_are_searchable_fields` | 設計 §15 の相関項目・agent 共通項目が検索できる型で mapping にある（既存へ写したものは写し先の型） | mapping は契約から生成されるので、契約の型を見れば足りる | 項目を `attributes`（検索対象外）にしか置かず、監視（§15）で絞り込めない |
| `test_new_field_names_do_not_look_like_secrets` | 件数・費用のフィールドが安全化の鍵名規則（§7.2 の `token` 部分一致）で伏せられない | 規則の関数を直接呼ぶ | `token_usage` という名前にして、`attributes` に落ちた時や文中の `key=value` で値が `[REDACTED]` になる |
| `test_actor_types_are_documented` | `actor_type` の語彙が log-contract.md にある（AGENTS.md §7） | 文書の文字列検査 | 語彙を足して文書を更新し忘れる |

## 2. 投影と再投影（`tests/unit/test_intervention_projection.py`、SQLite・実の JSON 整形器）

正本（`episode_events`）と投影（stdout の JSON）の境界を守る。OpenSearch は使わない（アプリは OpenSearch を
読まない・INV-38。Collector から先は隔離環境の試験 = 担当 D の phase 2）。

| テスト | 守る契約・事故 | 層の理由 | 捕まえる失敗 |
|---|---|---|---|
| `test_every_intervention_event_has_an_explicit_log_event` | 対応表が網羅的で同じ文字列 | 表の比較だけ | 新しい InterventionEvent を足して投影時に KeyError（握られて黙って出ない） |
| `test_event_id_is_deterministic_per_episode_and_seq` | 再投影が OpenSearch の同じ `_id` になる（設計 §14 の再構築） | 純粋関数 | ID が乱数・衝突して再投影が重複文書を作る／別の行を黙って捨てる |
| `test_formatter_uses_a_supplied_projection_event_id_only_when_well_formed` | 整形器は決定的 ID を 32桁 hex の時だけ採用 | 実の整形器を通す | 任意の文字列が `_id` になり Collector の退避・自動 ID に化ける |
| `test_append_event_is_logged_only_after_commit` | log-contract §9 と同じ commit 後の発行・相関項目 | store → session → 整形器の実経路 | commit 前に出て、rollback した変更がログにだけ残る |
| `test_rolled_back_event_is_never_logged` | 同上（rollback） | 同上 | 正本に無いイベントが検索に出る |
| `test_only_reference_like_refs_reach_the_log` | INV-39（本文・prompt を出さない） | 実の整形器の出力文字列を検査 | 他担当が refs に本文を入れた時にログへ流れる |
| `test_logging_failure_does_not_break_the_commit` | INV-38（ログの故障で正本の書き込みが失敗しない） | 故障を注入して commit を見る | 投影の例外が `append_event` を通して業務の transaction を壊す |
| `test_reproject_reemits_from_the_db_with_the_same_ids_and_marks_rows` | 再投影は live と同じ ID・`projected_at` を付け、2回目は出さない | DB → 整形器の実経路 | 再投影が別 ID で重複する／未投影の印が付かず毎回全件出す |
| `test_reproject_can_select_an_episode_and_a_seq_range` / `test_reproject_pages_through_more_rows_than_the_batch` | 範囲指定・頁送り（keyset） | 同上 | 頁の境界で行を落とす・重複する／他 Episode を出す |
| `test_reproject_rejects_a_seq_range_without_an_episode` / `test_script_refuses_a_seq_range_without_an_episode` | seq は Episode 内の通番 | 引数検査 | 全 Episode に seq 範囲を当てて意図しない行を出す |
| `test_script_reprojects_unprojected_rows_then_nothing` | `scripts/intervention-reproject.py` の既定（未投影だけ）・`--dry-run`・`--all` | script を読み込み SQLite の factory を渡す（Settings を読まない） | 運用手順（runbook §3）の command が実装と食い違う |
