# fal 月間 hard cap（ADR-0063 / INV-86）のテスト設計根拠

要件: `FAL_MONTHLY_BUDGET_USD`（正の値）は on/off ではなく**月間の上限**。新しい有料予約は
`month_counted + estimate <= cap` のときだけ作られる（ちょうど等しいは許可）。超過は
予約行を作らず・fal を呼ばず、既存の credit-hold（`WAITING_FOR_CREDITS` / `needs_input`）へ流れる。
判定と INSERT は同じ DB トランザクション（月 advisory lock）で、並行しても超えない。
fake provider と SQLite / テスト専用 PostgreSQL だけを使い、実 fal・本番 DB には到達しない。

共有の種まきは `tests/support/monthly_cap.py`。期待値は各テストが明示し、数える規則はそこに複製しない
（費用区分は `domain/provider/cost.py` から import）。

## 純粋判定（`tests/unit/test_budget_cap_domain.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_within_cap_...` / `test_exactly_at_the_cap_is_allowed` / `test_one_unit_over_...` | 境界 `<=`。`<` に取り違えると cap ちょうどが拒否され、`<=` を落とすと 1 単位超えが通る |
| `test_already_over_the_cap_denies_even_a_zero_estimate_and_clamps_remaining` | 既に超過した月は 0 見積でも拒否。`remaining` が負にならない |
| `test_decimal_arithmetic_is_exact_not_float` | 0.1 + 0.2 <= 0.3 が Decimal で厳密に成り立つ。float 混入で端数に拒否/許可がぶれる |
| `test_negative_or_non_finite_inputs_fail_closed` | 負・NaN・Infinity は `invalid_input` で拒否。壊れた値が無制限に化けない |
| `test_budget_decision_carries_the_cap_only_when_enabled` | `cap_usd` は ENABLED のときだけ。0/未設定/不正の既存挙動は不変 |
| `test_the_exceeded_error_is_a_budget_hold_and_therefore_needs_input` | 超過がクラッシュ（retry 嵐）でなく `needs_input` の保留経路に乗る型 |

## 台帳 reserve（`tests/unit/test_monthly_cap_ledger.py`、SQLite）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| 範囲内 / ちょうど cap / 超過 / 空の月に単発超過 | 許可・境界・拒否。拒否では行が増えず、例外が `month_counted/estimate/cap/remaining` を運ぶ |
| `test_no_cap_argument_keeps_the_old_behaviour` | `monthly_cap_usd=None` は検査なし（既存呼び出し・既存テストの後方互換） |
| `test_every_cost_state_counts_except_not_charged`（7 区分） | 数える: 未 dispatch の reserved・受理済み・evidence 確定・課金前提・曖昧・abandoned。数えない: 受理されなかった確定（`not_accepted`）だけ。安全側 |
| `test_seeded_rows_have_the_intended_cost_state` | 種まきが意図の費用区分を作れている（テスト自身のバグで上が空振りしない） |
| `test_both_fal_providers_...` / `test_non_fal_providers_are_not_counted` | image + video は 1 つの fal 族 cap を共有。codex の見積は混ざらない |
| `test_two_episodes_share_the_single_fal_cap` | 台帳に brand/account 列は無く、Episode をまたいで 1 本の cap を共有（複数ブランド想定） |
| 見積 NULL（新規 / 数える既存行）→ `unknown_cost` | 費用不明は拒否（fail closed）。ただし not_charged の NULL は解放済みなので不明扱いにしない |
| `test_a_replayed_key_is_neither_double_counted_nor_cap_denied` | 同じ idempotency key の再実行を cap で拒否せず・二重計上しない |
| `test_a_reconciled_not_charged_reservation_is_released_...` | 曖昧で数えていた行が `not_accepted` で確定すると枠が戻る |
| `test_an_operator_failure_resolution_stays_counted_...` / `test_counting_uses_the_estimate_...` | 実額列が無いので見積のまま数える（過大計上は安全側）。手動 failure 照合は課金前提 |
| 月替わり（前月末 / 月初ちょうど / JST 境界 / 12→1 月） | UTC の月で数える。前月の行は数えず、月初ちょうどは今月。ローカル時刻で切ると月の境目が 9 時間ずれる |
| `test_provider_credits_month_spend_matches_what_the_gate_counts` | status / resume の月間費用とゲートの `month_counted` が同一定義（未 dispatch の予約を含む） |
| 月ロック順・鍵・SQL 集計 | 月ロック → 入力ロックの固定順（デッドロック回避）、鍵は `sha256("fal-monthly-cap|YYYY-MM")`、cap 無しなら月ロックを取らない、合計は SQL の `SUM` |
| `test_the_decision_function_is_the_one_in_the_domain` | reserve の結論が `evaluate_cap` と一致（境界の取り違え防止） |

## PaidJobRunner（`tests/unit/test_paid_job_monthly_cap.py`、SQLite）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| 範囲内 / ちょうど cap | 通常の submit。見積が台帳に残る |
| `test_over_the_cap_holds_for_credits_with_no_row_and_no_fal_call` | 超過で submit 0 回・予約行なし・`WAITING_FOR_CREDITS` の保留（`decision_reason=monthly_cap_exceeded`）・breaker は closed・`provider.call.suppressed` に数値が出る |
| `test_the_cap_hold_is_idempotent_...` | 何度止まっても保留 1 行。予算を上げて worker を作り直せば同じ spec が通る（resume の前提） |
| `test_the_cap_covers_the_video_path_too` | 動画経路でも同じ cap（image/video 共有） |
| `test_an_unknown_estimate_fails_closed` / `test_a_null_estimate_row_...` | 見積が出せない・NaN・負・既存行 NULL は保留（fal 0 回） |
| `test_replaying_the_same_activity_is_not_cap_denied_nor_double_counted` | worker 再起動 / Temporal 再実行で cap ちょうどでも拒否されず、fal 再送 0 回、行は 1 つ |
| `test_a_crash_between_reserve_and_dispatch_resumes_without_a_cap_check` | reserve commit 後に落ちた未 dispatch 行は、既に月に入っているので再開時に cap で止めない（pre-check が既存行を弾く不具合を検出する） |
| `test_switching_the_budget_to_zero_blocks_new_reservations_only` | 予算 0 は新規のみ拒否。飛行中の予約は触らない |
| `test_failed_submits_are_counted_or_released_...`（timeout / 422 / 404 / 残高拒否） | timeout は曖昧、422・404 は課金前提で spent、いずれも見積のまま数える。残高拒否（`not_accepted`）だけ解放 |

## PostgreSQL 並行（`tests/integration/test_monthly_cap_concurrency.py`、`*_test` URL が無ければ skip）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_concurrent_reservations_never_exceed_the_cap`（2/5/12 本） | 月ロックが無いと check-then-act で合算が cap を超える。成功は ちょうど 3 本・合計 <= 60 |
| `test_two_racers_for_the_last_slot_admit_exactly_one` | 残り 1 枠の取り合いで片方だけ |
| `test_image_and_video_racers_share_the_one_family_cap` | provider が違っても同じ月ロック |
| `test_racing_replays_of_one_key_...` | 同一 key の同時実行で cap 拒否も二重計上もしない |
| `test_mixed_input_and_month_locks_do_not_deadlock` | 同じ入力に重なっても固定順でデッドロックしない |

## 契約（`tests/contract/test_fal_monthly_cap_wiring.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| reserve の署名 / `paid_job` だけが fal を予約でき、cap を渡す | cap を持たない fal の予約経路が増えると上限が黙って効かなくなる。codex/youtube の台帳呼び出しは fal を名指ししない |
| provider 集合・月初関数が 1 箇所 | AGENTS §8。`replace(day=1…)` は production に 1 つ。`provider_credits.month_start` はそれと同じ振る舞い |
| cap 算術に float が無い | 端数誤差で境界がぶれない |
| workflow / payload が cap を知らない | Temporal の replay 互換（INV-40）。既存の replay テスト（`test_daily_watchdog_replay.py` ほか）が履歴 fixture でそれを担保する |
