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
