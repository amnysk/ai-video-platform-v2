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
