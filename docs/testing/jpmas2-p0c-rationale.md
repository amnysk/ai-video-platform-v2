# JP-MAS-002 P0-C（Tool Gateway・費用・予算 gate・Feature Flag）のテストの理由（ADR-0043 §2・§5）

テストを足した理由を、足したのと同じコミットで残す。

## 1. Tool Gateway と費用（tests/unit/test_agent_tool_gateway.py）

- `test_llm_call_cost_is_token_usage_times_the_unit_price`: 費用の式（usage × 1k 単価）を固定する。
- `test_unknown_usage_gives_none_not_zero` / `test_a_policy_without_prices_cannot_measure_cost` /
  `test_sum_costs_is_none_when_any_part_is_unknown`: 欠測を 0 にしない（INV-56）。0 にすると予算の判断と
  運用の可視化が「安い」と誤る。Codex の policy は単価が無い。
- `test_an_unlisted_tool_is_refused_before_anything_is_called` / `test_a_listed_but_unprovided_tool_is_refused` /
  `test_the_llm_tool_cannot_go_through_call` / `test_llm_json_is_refused_when_the_spec_does_not_list_it`:
  handler が spec の外へ出ない。拒否は呼ぶ前（副作用が起きない）で、拒否も記録に残る。
- `test_the_llm_call_cap_is_enforced_before_the_next_call`: `max_llm_calls` の強制と `max_tokens` の受け渡し。
- `test_gateway_errors_are_permanent_failures`: 同じ spec・handler では必ず同じ結果なので retry しない分類。
- `test_a_measured_run_reports_tokens_and_cost` / `test_unknown_usage_makes_tokens_and_cost_none_not_zero` /
  `test_an_unpriced_policy_gives_none_cost_but_known_tokens` /
  `test_a_failed_llm_call_counts_against_the_cap_and_makes_cost_unknown`: 合計は測れた分だけを出さない。
  失敗した呼び出し（課金されたかもしれない）は「不明」として数え、上限にも数える。
- `test_invocations_are_json_safe_and_never_contain_the_prompt`: prompt 本文は INV-39 でログ・行に出せない。
- `test_a_run_without_llm_calls_has_a_known_zero`: 「呼んでいない」は不明ではなく既知の 0。
- `test_registered_specs_reference_registered_policies`: Registry の spec が未登録の policy を指さない。

## 2. 予算 gate（tests/unit/test_agent_budget_gate.py）

- `test_the_first_call_reserves_the_ceiling_then_dispatches`: 予約は上限で、予約 → dispatch の順（INV-15）。
- `test_without_a_usd_jpy_rate_...` / `test_exceeding_the_episode_budget_...`: 検証不能・超過のどちらも、
  予約を作る前に拒否する（拒否されたのに台帳に残る状態を作らない）。
- `test_earlier_ceilings_count_against_the_budget`: 実績が測れなくても上限が予算に計上される。
- `test_a_held_episode_refuses_dispatch_and_the_reservation_stays_undispatched`: INV-44。送っていない証拠が残る。
- `test_a_replay_returns_the_same_reservation_without_a_second_budget_check` /
  `test_a_held_then_resumed_replay_dispatches_the_same_reservation`: Activity の再実行で二重予約・再検査をしない。
- `test_an_unreconciled_reservation_blocks_a_new_key` /
  `test_settling_with_evidence_closes_the_reservation_and_unblocks_the_next` /
  `test_a_failed_call_is_settled_conservatively` /
  `test_settling_a_success_without_evidence_is_a_programming_error`: ADR-0013 の照合規則を Codex 経路と揃える。
- `test_the_policy_must_be_the_one_the_spec_pins`: spec が固定した policy 以外で予約しない。

## 3. LLM 実装とハンドラ契約（tests/unit/test_agent_llms.py）

- fake の決定性・token 報告・応答なしを黙って空にしない: E2E と単体テストが再現可能であるため。
- Codex アダプタ: schema を渡す・usage は JSONL に在るときだけ・読めなければ None・fence を許し object 以外は拒否。
  実 CLI は呼ばない（`StoryGenerator` の偽物）。
- `test_handler_input_names_cover_every_registered_handler`: 入力名の正本が `AgentHandlerRef` と同じ集合。
- `test_an_output_needs_an_explicit_outcome`: outcome の取り違え（暗黙の produced）を防ぐ。

## 4. RunCapture の費用（tests/unit/test_agent_run_cost_capture.py）

- 従来の capture は NULL のまま・snapshot に項目を足さない（従来経路を変えない）。不明な費用は NULL。
  既知の費用と `agent_spec` は保存される。

## 5. Flag（tests/unit/test_agentic_flags.py、tests/architecture/test_agentic_default_off.py）

- 既定オフ・カンマ区切りの解析・未知の role は起動時に失敗・`codex`/`fake` 以外は拒否（INV-55）。
- 日次経路が agentic のモジュールを import しない。`workers/planning` は `CreativeBrief` だけ許す。検査自体も
  import の書き方ごとに確かめる（検査が空振りしないこと）。
