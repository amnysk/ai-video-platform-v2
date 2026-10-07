# 介入の適用（JP-MAS-001 担当 B）のテストの理由

ADR-0041、契約は `docs/domain/intervention.md` §2–§9。担当 B が持つ不変条件は INV-41〜44・INV-46。
単体テストは SQLite（`tests/conftest.py`）と Temporal の time-skipping 環境で、外部（LLM・fal・YouTube）は
呼ばない。PostgreSQL の行 lock でしか起きない競合（hold と dispatch）は
`tests/integration/test_intervention_gate_race.py` に書き、隔離環境（担当 D）で実行する。

## dispatch gate（`tests/unit/test_intervention_gate.py`）

守る契約: 有料 dispatch・投稿の開始許可は、Episode の制御状態と必須記録を**同じ transaction**で照合する
（INV-44 / INV-46）。前身 repo の「停止したつもりで課金が続いた」型の事故を、台帳の境界1か所で止める。

- `test_an_episode_without_a_control_row_dispatches_and_nothing_is_written`: 日次・旧 Episode は行を作らず通る。
  gate が行を作ると全 Episode に制御行が増え、日次経路の挙動（とデータ）が変わる。
- `test_a_non_running_episode_refuses_dispatch_and_leaves_the_reservation_unsent`（4状態）: running 以外は拒否し、
  予約は `reserved` かつ `dispatched_at IS NULL`（送っていない証拠）。ここが崩れると resume 時に二重課金か
  人手照合待ちになる。
- `test_upload_dispatch_is_refused_while_held_but_an_in_flight_session_is_not_reopened`: 投稿は新しい session の
  開始だけを拒否し、dispatch 済み session の再入（in-flight）は拒否しない。再入まで拒否すると受理位置が分からなく
  なる経路を増やす。
- `test_new_mode_requires_a_record_for_the_current_script_and_storyboard` /
  `test_legacy_mode_with_a_row_does_not_require_records`: INV-46。記録の種類（agent_run・decision_record）の
  どちらでも足り、legacy は要求しないこと。
- `test_a_reservation_refused_by_the_gate_is_reused_after_resume`: 実際の `PaidJobRunner` で、拒否された予約が
  resume 後に**同じ予約 id**で dispatch され、予約が1行のままであること（provider の submit は1回）。gate を
  単体で見るだけでは、既存の冪等経路（`_plan_round` の「reserved + 未 dispatch」分岐）と噛み合うかが分からない。

## command の受付と適用（`tests/unit/test_intervention_commands.py`）

守る契約: command 行は transactional outbox で、受付は1 transaction、適用は制御行の lock の下で冪等
（INV-42/43/44、`docs/domain/intervention.md` §4）。Temporal を使わず `apply_command`（Activity の本体）を
直接呼ぶ。配送の重複・Worker 障害は「同じ関数を二度呼ぶ」に還元できるので、この水準で検査できる。

- `test_hold_is_requested_in_the_receiving_transaction_and_not_reported_as_done`: hold は受付の commit で
  `hold_requested`（dispatch gate が効く）だが、command は `received` のまま（完了と表示しない）。
- `test_same_id_and_payload_returns_the_existing_command` / `test_same_id_with_another_payload_is_refused`:
  クライアントの再送は行もイベントも増やさず、id の使い回しは 409 になる。
- `test_receive_refuses_missing_scope_ai_set_mode_bad_payload_and_unknown_episode`: 受付で拒否するものは
  行を作らない（拒否が outbox に残って再配送されない）。`set_mode` は scope を持っていても AI には許さない。
- `test_comment_is_applied_at_receipt_and_reapply_returns_the_stored_result` /
  `test_applying_the_same_command_twice_is_one_logical_change`: 終端済みの command の再適用は保存済み result を
  返すだけで、制御版・時刻・イベント数が変わらない（apply の commit 後に Worker が落ちた場合の復旧）。
- `test_user_constraint_is_recorded_with_verifiable_target_ids`: 制約は自由記述でなく検証できる対象 id を持つ。
- `test_hold_becomes_held_and_lists_in_flight_unknown_and_undispatched`: HELD の応答が予約台帳の3分類を
  正しく分け（spent は載せない）、provider の job 参照を出さない。
- `test_a_stale_expected_control_revision_is_a_conflict_and_changes_nothing`: INV-43。resume も hold も
  期待版が古ければ conflict で、状態を変えない。
- `test_resume_runs_again_from_the_next_stage`: resume の開始工程と pipeline の start 入力、follow-up の記録後は
  再配送が pipeline を二度 start しない。
- `test_resume_is_refused_while_an_unknown_outcome_reservation_exists`: 成否不明は人の照合が先（INV-15）。
- `test_release_checkpoint_binds_the_approval_to_the_active_manifest`: 古い manifest への承認は conflict、
  現在の manifest への承認はその manifest に結び付く。
- `test_set_mode_by_a_human_uses_the_default_co_production_points` /
  `test_cancel_is_recorded_at_receipt_and_cannot_be_resumed`: mode の既定地点、cancel は記録だけで resume 不可。
