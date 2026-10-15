# 対話 AI アシスタント adapter のテストの理由（JP-MAS-001 / ADR-0042）

テストを足した理由を、足したのと同じコミットで残す。

## 1. `intervention.api.request`（tests/unit/test_assistant_request_log.py）

- `test_a_request_is_logged_with_route_actor_status_and_correlation`: 「どの AI セッションのどの tool 呼び出しが
  どの変更を起こしたか」を OpenSearch 側から辿る相関（ADR-0042 §6）。route は実 path でなく template
  （episode_id が cardinality を増やさない）、主体は認証が成功した principal の actor_type／actor_id。
- `test_neither_token_nor_authorization_nor_body_is_logged`: INV-39。介入 API は本文（コメント）と
  Bearer token を扱う。middleware がそれらを読まないことを、実際の JSON 整形器の出力と caplog の両方で見る。
- `test_a_rejected_request_is_logged_without_an_actor`: 401 でも要求は記録する（不正アクセスの痕跡）が、
  誰かを推測して書かない・誤った token を写さない。
- `test_unsafe_header_values_are_not_logged`: 相関 ID・tool 名はクライアント入力。改行・空白を含む値を
  そのままログへ入れるとログ注入になるので、安全な文字だけのときに限る。
- `test_only_the_intervention_prefix_is_logged_as_intervention`: 他の route（health 等）の既存ログを変えない。

## 2. 判断の説明と横断検索（tests/unit/test_assistant_insights.py）

- `test_explanation_is_built_from_stored_rows` / `test_interventions_target_only_the_decisions_revisions`:
  説明が保存済みの行（decision・agent_run・注記・command・revision_links）から組み立てられ、別の版への
  コメントを混ぜないこと。
- `test_reported_rationale_is_labelled_self_reported_not_verified`: モデルの自己申告を検証済みの事実として
  表示しない（ADR-0042 §5、設計 §7）。`rationale_kind` を言い換えない。
- `test_missing_data_is_reported_as_not_recorded_never_filled` /
  `test_a_decision_without_rationale_or_agent_run_lists_what_is_missing`: 欠測を補完しない。token・費用が NULL
  のとき 0 ではなく null と欠測一覧で返す（ADR-0041 の負債の可視化）。
- `test_recorded_token_usage_and_cost_are_shown_as_recorded`: 上の裏。記録があれば欠測にしない。
- `test_unknown_or_foreign_decision_is_404`: 他 Episode の判断を別 Episode の経路で読ませない。
- `test_explanation_requires_read_scope` / `test_search_requires_read_scope`: 新 route も介入 API の認証を通る。
- `test_search_filters_by_type_role_and_episode`: 横断検索の条件（役割は agent_runs 経由）。不正な役割は 422。
- `test_search_paginates_with_a_keyset_cursor_and_caps_the_limit`: 欠落・重複のないページング、limit の上限、
  不正な cursor の拒否。
- `test_the_new_routes_are_read_only_gets`: INV-47 の「書き込みは command の受付だけ」を新 route も守る。
