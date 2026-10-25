# fal 予算 0 / 未設定の保留（ADR-0061 / INV-84）のテスト設計根拠

要件: 予算 0 でも日次 Workflow が失敗せず、fal へ有料 submit を 1 回もせずに正常終了する。
未設定は無制限ではない。予算を足しても未照合予約は再送されず、完成済みシーンは再生成されない。
fake / time-skipping Temporal だけを使い、fal・Codex・YouTube・OpenSearch には一切到達しない。

## 純粋判定（`tests/unit/test_budget_gate_domain.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_explicit_zero_is_disabled_and_holds_without_any_provider_call` | 明示的な 0 が `DISABLED` で submit 不可。ここが崩れると予算 0 で fal へ行く |
| `test_unset_is_not_unlimited` | 未設定を無制限と読まない。既存の挙動（未設定でも submit）に戻らない |
| `test_invalid_values_fail_closed_as_unset` | 負・NaN・Infinity は安全側。壊れた設定で課金が始まらない |
| `test_positive_budget_passes_this_gate_only` | 正の値は「この関門を通る」だけ（累計判定は Phase 2）。過剰に止めない |
| `test_decision_reason_is_machine_readable_and_secret_free` | ログ・保留の `decision_reason` が `budget_*` の機械値 |

## PaidJobRunner（`tests/unit/test_paid_job_budget.py`、SQLite）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_zero_or_unset_budget_never_reserves_prepares_or_submits` | 予算 0 / 未設定 / 不正 × 画像 / 動画で、`submit` 0 回・`prepare` 0 回（元画像 upload も無い）・台帳に何も残らない・保留に機械判定値が残り・breaker は閉じたまま |
| `test_budget_held_error_is_needs_input_so_the_workflow_ends_normally` | 例外が `needs_input` に分類される。落ちると Activity の retry 嵐か Daily の失敗になる |
| `test_budget_hold_is_idempotent_per_scene` | 同じシーンで何度止まっても保留は 1 行 |
| `test_positive_budget_submits_normally` | 対照: 正の予算では従来どおり submit |
| `test_already_accepted_job_is_not_resubmitted_when_budget_drops_to_zero` | 受理済みジョブの再入は新規 submit ではない。予算 0 でも再 submit は 0 回（二重課金の防止） |
| `test_unreconciled_reservation_is_never_resent_after_budget_is_added` | 予算が正になっただけで、未照合予約は再送されない（INV-67） |
| `test_a_broken_log_pipeline_does_not_change_the_held_outcome` | ログ基盤（OpenSearch）が止まっても結果は同じ型・fal へ行かない（INV-38） |

## 日次 Workflow（`tests/unit/test_budget_zero_daily_workflows.py`、time-skipping Temporal）

`test_daily_credit_hold_workflows.py` の fake 工程・本物のゲート Activity を再利用し、breaker は **closed のまま**。

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_zero_or_unset_budget_plans_scripts_then_holds_and_ends_normally` | 企画・台本・絵コンテは進み、production は始まらず `HELD`（完成ではない）。Daily は例外なく終わる |
| `test_positive_budget_runs_the_whole_pipeline_as_before` | 対照: 正の予算では最後まで走る |
| `test_a_month_of_zero_budget_never_fails_and_is_bounded` | 28 日続けても Daily は 1 度も失敗せず、Episode は 2 件・Codex 2 回・有料工程 0。3 日目以降は `HELD_BACKLOG` |
| `test_held_backlog_reason_and_result_are_machine_readable` | 3 日目の結果が `HELD_BACKLOG` で Episode を作らない |
| `test_gate_activity_results_carry_the_budget_status` | ゲートの結果に `budget_status` / `hold_reason` が載る（監視が読める） |
| `test_budget_hold_without_a_writer_refuses_rather_than_going_on_to_paid_work` | 保留の書き手が未結線なら黙って通さず例外にする |
| `test_hold_is_idempotent_when_the_production_gate_is_called_twice` | gate の再実行（Activity retry）で保留が増えない |

既存テストの変更（理由: Protocol に省略可の引数を足したことへの追随。緩めていない）:
`tests/unit/test_daily_credit_hold_workflows.py` の `FakeProviderHealth.hold_episode` が `detail` を受けて
`Hold.detail` に残す。`tests/contract/test_compose_workers.py::test_per_worker_minimal_env` に
`FAL_MONTHLY_BUDGET_USD` の行を足した（持つ worker は 3 つだけ）。

## 設定と結線（`tests/contract/test_fal_budget_wiring.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_budget_setting_is_read_fail_closed` | 未設定・空・不正・NaN/inf・負が全て submit 不可、0 は `DISABLED`、正は `ENABLED`。**`abc` / `nan` / `inf` は pydantic が Decimal を拒否して `Settings()` ごと落ちる実バグを実装中に捕捉した**（worker が起動しなくなる）。空文字は compose の `${VAR:-}` が渡す |
| `test_every_paid_worker_passes_the_budget_to_its_runner` / `test_the_pipeline_worker_passes_the_budget_to_the_gates` | 結線の欠落（`PaidJobRunner` の `budget=None` は未設定＝止まる。回帰は `test_runner_built_without_a_budget_fails_closed`）を本番の入口で検出する |
| `test_every_runner_construction_in_production_code_is_in_a_wired_worker` | `PaidJobRunner` を組む新しい入口が増えたら、結線の確認なしに通れない |
| `tests/contract/test_compose_workers.py::test_per_worker_minimal_env` | 予算の環境変数が compose から 3 worker にだけ届く（届かないと永久に UNSET） |

## watchdog（`tests/unit/test_hold_watchdog_classification.py`）

| テスト | 守るもの / 落ちたら何が起きているか |
|---|---|
| `test_budget_holds_are_held_not_failed_and_stay_visible` | 予算由来の保留が失敗として通報されず、`held_episodes` には見える（INV-77） |

## 実行していないもの

- `tests/integration/*`（Docker Compose の PostgreSQL / Temporal が要る）。migration・状態遷移・Workflow の
  コードは変えていないので、既存の replay（`test_daily_credit_hold_workflows.py` の旧履歴 fixture、
  `test_log_workflow_replay.py`）が通ることを unit で確認した。
- 本番 Temporal の履歴の replay（読み取り専用 export）。所有者の環境で行う。
