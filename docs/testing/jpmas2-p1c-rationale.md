# JP-MAS-002 P1-C（AgenticEpisodeWorkflow・plan pin・draft/start・Learning）のテストの理由

テストを足した理由を、足したのと同じコミットで残す（ADR-0043 §6）。

## 1. Template コンパイル入口と script brief 変換（tests/unit/test_agentic_plan_compile.py）

- `test_shorts_agentic_compiles_to_an_agentic_plan`: agentic の入口が実際の `shorts_agentic@1` を通ること。
- `test_unknown_unrunnable_and_legacy_templates_are_refused`: 未知・`runnable=False`・`daily_legacy`
  （compile は通るが agentic では走らせない）を、起動側（pin・draft・start）が同じ code で拒否する根拠（INV-53）。
- `test_every_other_compile_error_is_an_invalid_payload`: 写像表に無い code が黙って成功に化けない。
- `test_every_required_existing_stage_maps_to_a_pipeline_stage`: workflow が既存工程を起動する写像が、
  Template の必須工程順（`REQUIRED_STAGE_ORDER`）と `PipelineStage` の定義順から食い違わない。
- `test_a_creative_brief_script_brief_matches_the_plan_brief_without_a_plan_id`: CreativeBrief 由来の台本 brief は
  plan_brief と同じ題材の列・locale・形式で、TopicPlan id だけが None（TopicPlan を作らない）。
- `test_a_creative_brief_with_an_unknown_profile_is_refused_not_defaulted`: 未知の profile を黙って既定へ
  落とさない（plan_brief と同じ失敗安全）。

## 2. pipeline 側 Activity（tests/unit/test_agentic_pipeline_activities.py）

- `test_pin_compiles_stores_and_records_the_plan`: 計画が `workflow_plan` Artifact と判断記録
  （`decision.recorded`）として残る。後から「どの計画で走ったか」を辿れる。
- `test_pin_reports_the_retry_limit_of_each_pinned_agent_spec`: workflow は Registry を読まないので、
  agent stage の retry 上限は pin 時に Activity が履歴へ残す。
- `test_a_pinned_plan_is_returned_unchanged_after_the_template_changes`: INV-51 の実行時の側。実行開始後に
  TEMPLATES が変わっても同じ Episode の計画は変わらず、別 Episode は新しい Template を使う。
- `test_pin_is_idempotent_and_writes_one_artifact_and_one_decision`: Activity の再試行・再送で
  Artifact・判断記録が増えない。
- `test_pin_refuses_legacy_unrunnable_and_unknown_templates`: `daily_legacy`・`runnable=False`・未知を
  非再試行の code 付きで拒否し、何も書かない（INV-53）。
- `test_pin_refuses_a_missing_episode`: 存在しない Episode に計画を作らない。
- `test_load_returns_the_pinned_plan_or_refuses`: Learning が新しい計画を作らず、pin 済みだけを読む。
- `test_adopt_chooses_the_recommended_candidate_by_rule`: producer は推奨候補を規則で採用し、判断記録
  （`choose_topic`、actor `agent:producer`、`rationale_kind=rule`、候補は brief から）と topic を書く。
  採用されなかった候補の理由は rejected なら brief の理由、それ以外は規則の事実だけで補完しない。
- `test_adopt_is_idempotent_and_never_creates_a_topic_plan`: 再実行で判断記録が増えず、TopicPlan 行を
  作らない（日次 Planner との衝突を避ける。ADR-0043）。
- `test_adopt_without_a_strategy_brief_stops_and_changes_nothing`: 入力が無いのに topic を書き換えない。
- `test_adopt_refuses_a_brief_written_for_another_episode`: 取り違えた brief を採用しない。

## 3. AgenticEpisodeWorkflow / AgenticLearningWorkflow（tests/unit/test_agentic_workflow.py）

time-skipping 環境で、子 workflow と `run_agent_stage` は fake、control gate・upload gate・pin・adopt は本物。

- `test_shorts_agentic_runs_the_stages_in_plan_order`: `shorts_agentic@1` の実行順（strategy -> producer ->
  creative -> script ... private_upload -> quality）と、producer が topic を書き TopicPlan を作らないこと。
- `test_existing_stages_use_the_same_ids_queues_and_payloads_as_the_pipeline`: 既存工程が
  `EpisodePipelineWorkflow` と同じ子 workflow id・入力で起動される（二重起動の規則・resume が共通）。
- `test_agent_stage_requests_carry_the_pinned_spec_and_plan_hash`: agent worker へ渡すのは pin した
  spec の版と hash と plan_hash（冪等キー・`agent_spec_drift` 検査の入力）。
- `test_quality_runs_exactly_once_and_proposals_are_only_counted`: quality は1度だけ・後ろ向きの辺が無く、
  提案は数えるだけで自動適用や再実行をしない（INV-57 の workflow 側）。
- `test_the_pinned_plan_is_used_after_the_template_changes`（INV-51 実行時）: 途中で止まった Episode は
  Template が変わっても元の計画で続く。別 Episode は新しい計画。
- `test_unknown_unrunnable_and_legacy_templates_are_refused_before_any_stage`: 未知・未実装・legacy は
  どの stage も始めずに理由付きで止まる（INV-53）。
- `test_a_drifted_agent_spec_stops_without_retrying`: `agent_spec_drift` は再試行せず、理由を残して止まる。
- `test_a_failing_agent_stage_is_retried_up_to_the_pinned_spec_limit`: retry 回数は pin 時に履歴へ残した
  spec の `retry_max_attempts` で、無制限の再試行（有料呼び出しの繰り返し）にならない。
- `test_a_held_episode_parks_before_the_first_stage`: hold 中は何も起動せず `parked`（失敗ではない）。
- `test_a_blocked_or_rejected_agent_stage_stops_with_a_recorded_reason`: blocked / rejected_output を
  黙って飛ばさず、理由付きで止まり、後続を起動しない。
- `test_the_producer_stops_the_workflow_when_there_is_nothing_to_adopt`: StrategyBrief が無いのに先へ進まない。
- `test_a_child_that_parks_elsewhere_stops_the_workflow`: 駐機点以外の status は止まる（既存と同じ判定）。
- `test_the_upload_gate_is_checked_before_the_upload_and_refusal_stops`: 投稿の前に upload gate を通す
  （private 投稿の保護、AGENTS.md §9）。拒否されたら投稿せず、quality も走らない。
- `test_learning_runs_the_pinned_post_publish_stage_and_returns_the_outcome_as_is` /
  `test_learning_returns_the_lesson_revision_when_one_was_produced` /
  `test_learning_without_a_pinned_plan_is_blocked_and_pins_nothing`: Learning は pin 済みの post_publish を
  読むだけ（新しい計画を作らない）、`awaiting_public_metrics` を言い換えない。
- `test_legacy_histories_still_replay_with_the_agentic_workflows_registered` /
  `test_a_history_produced_by_the_legacy_workflow_replays`: 日次の workflow の履歴は、agentic の workflow を
  同じ worker に載せても replay できる（INV-55、replay 互換）。
- `test_the_legacy_workflow_module_does_not_mention_the_agentic_workflow`: `EpisodePipelineWorkflow` の
  ファイルに agentic の語が入らない。

## 4. worker 登録（tests/unit/test_agentic_worker_registration.py）

- `test_the_default_registration_is_exactly_the_daily_workflows_and_activities`: 既定の pipeline worker の
  登録（workflow 3 つ・Activity 名）が従来と同一（INV-55）。
- `test_the_agentic_registration_adds_only_the_agentic_workflows_and_activities`: flag 有効時に足すのは
  agentic の 2 workflow と 3 Activity だけ。
- `test_the_agentic_activities_are_exactly_the_contract_names`: Activity 名の宣言元（contracts）と実装の一致。
- `test_main_builds_the_agentic_activities_only_behind_the_flag`: `main` が flag の内側でだけ agentic を
  構成する（既定の起動は MinIO にも触れない）。
