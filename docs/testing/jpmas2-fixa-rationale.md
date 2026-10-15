# JP-MAS-002 fix A: テスト設計の理由

ADR-0043 §Consequences の負債2件（台本の再利用キーに CreativeBrief の版が無い / 介入 resume が
`AgenticEpisodeWorkflow` を再起動しない）を解消した変更のテスト。

## 1. 台本の再利用キー（tests/unit/test_script_brief_revision.py）

- `test_no_brief_revision_keeps_the_hash_and_a_revision_changes_it`（INV-55）: `None` は `input_hash` を
  そのまま返す（plan のある / brief の無い Episode の値は不変）。版が違えば別の hash、同じ版なら同じ hash。
- `test_a_script_made_under_an_older_creative_brief_is_not_adopted`: brief 不変なら再利用（二重生成・二重課金
  しない）、brief の内容が変わったら古い台本を採用せず再生成する。実際の `generate_script` で検査する
  （定義だけでなく、reuse の判定が hash を使っていること）。

## 2. agentic Episode の resume（ADR-0043 負債。判定は Activity、workflow は結果で分岐）

tests/unit/test_intervention_agentic_resume.py（time-skipping の Temporal + SQLite、本物の apply Activity）:

- `test_resume_of_an_agentic_episode_starts_the_agentic_workflow_not_the_pipeline`: pin 済みの計画がある
  Episode の resume が `AgenticEpisodeWorkflow`（id `agentic-episode-{id}`、template と worker の queue 付き）を
  start し、`EpisodePipelineWorkflow` を start しない（agent stage が resume 後に走らない負債の本体）。
- `test_resume_of_a_legacy_episode_is_unchanged`（INV-55）: 計画の無い Episode は従来どおり pipeline、patched marker なし。
- `test_a_second_resume_while_the_agentic_run_is_open_reports_already_running`: 実行中の二重起動は
  `already_running`（同時に2本走らない）。
- `test_a_closed_agentic_run_can_be_started_again_by_the_next_resume`: 閉じた実行の id を再利用できる
  （「1度 parked したら二度と動かない」の再現防止）。
- `test_the_agentic_branch_is_patched_and_both_histories_replay`（INV-55）: 分岐は `workflow.patched` で守られ、
  legacy / agentic の両方の履歴が現行 code で replay できる。
- `test_a_history_recorded_before_the_agentic_branch_still_replays`: 分岐を足す前の code で記録した実履歴
  （fixtures/intervention_command_resume_pre_agentic_history.json）が replay できる。

tests/unit/test_resume_api_agentic.py:

- `test_resume_of_an_agentic_episode_starts_the_agentic_workflow` / `..._legacy_episode_never_touches_the_store`:
  API の resume も同じ決定をし、legacy は ArtifactStore に触らない（既存の resume テストを変えずに通る根拠）。
- `test_resume_of_an_agentic_episode_while_running_is_409`: 実行中は 409（ADR-0032 と同じ）。
- `test_the_api_starter_reuses_a_closed_agentic_id_but_never_a_running_one`: 再起動の id 再利用方針は
  ALLOW_DUPLICATE（実行中の拒否は Temporal が保証する）。

tests/unit/test_agentic_restart_idempotency.py / test_agentic_workflow.py（再起動で二重実行しない証明）:

- `test_a_restart_returns_the_stored_stage_without_a_new_llm_call_or_reservation`: 完了済みの agent stage は
  再起動で保存済みの結果を返し、LLM 呼び出し・予約・成果物・判断記録・run が1件も増えない（二重生成・二重課金なし）。
- `test_pinning_again_returns_the_same_plan_and_writes_nothing`: 再起動の pin は同じ計画（`existing=True`）で、
  計画の版も増えない（INV-51）。
- `test_a_parked_run_can_be_started_again_with_the_same_id_and_the_same_plan`: parked の後に同じ id を再起動すると
  同じ plan_hash で全工程を歩き、agent stage の要求に Learning の scope が付かない（冪等キーが不変）。

tests/integration/test_jpmas2_agentic_e2e.py::test_a_hold_then_resume_restarts_the_agentic_workflow_without_double_work:
実 PostgreSQL / Temporal / MinIO で、介入 command の resume が再起動し、再度の hold -> resume でも LLM・予約・
成果物・投稿が増えない。
