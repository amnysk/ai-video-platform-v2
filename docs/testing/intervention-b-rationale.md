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
