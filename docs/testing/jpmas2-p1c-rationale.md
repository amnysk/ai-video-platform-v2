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

## 5. 台本の brief 決定（tests/unit/test_script_creative_brief.py）

- `test_an_episode_with_a_creative_brief_and_no_plan_gets_its_subject_matter`: TopicPlan が無く現行の
  creative_brief がある Episode は、その題材の列（plan と同じ並び）で台本を作る。TopicPlan id は None。
- `test_the_creative_brief_reaches_the_generator_prompt`: 題材が実際に generator の prompt に載り、locale は
  strategy profile から決まる（英語）。
- `test_an_episode_without_a_creative_brief_keeps_the_legacy_brief_and_hash`（INV-55）: creative_brief の
  無い Episode の brief が `legacy_brief` と同一で、prompt の入力 hash も同じ（日次・手動は変わらない）。
- `test_an_episode_with_a_plan_keeps_the_plan_brief_even_with_a_creative_brief`: plan のある Episode は
  creative_brief があっても plan_brief のまま（日次が agentic に影響されない）。
- `test_an_unsupported_creative_brief_version_is_refused_not_read`（INV-54）: 未知の schema 版を読み替えず
  `PromptContractError`（needs_input、再試行しない）で拒否する。

## 6. use case・adapter（tests/unit/test_agentic_use_cases.py）

- `test_a_draft_is_an_idle_planned_episode_with_a_brief_and_an_intervenable_control`: draft は `planned` の
  Episode・UserBrief・`auto_intervenable` の制御行を作るだけで、workflow id も持たない（生成を始めない）。
- `test_a_draft_uses_no_daily_slot_and_no_topic_plan`: 日次の枠・TopicPlan を使わない（日次 Planner と衝突しない）。
- `test_resending_the_same_command_returns_the_same_episode`: 応答を失った再送が Episode を増やさない。
- `test_the_same_command_id_with_another_brief_is_command_id_reused`: 同じ id で別内容を黙って受けない。
- `test_different_commands_make_different_episodes`: id の決定が command ごとに分かれる。
- `test_a_draft_for_an_unknown_unrunnable_or_legacy_template_creates_nothing`: 起動できない template の
  draft を作らない（INV-53）。拒否 code は pin と同じ。
- `test_a_format_profile_other_than_the_templates_is_refused`: UserBrief と Template の形式の食い違い。
- `test_start_is_refused_when_the_flag_is_off`（INV-55）: flag が無効なら何も起動しない。
- `test_start_runs_the_agentic_workflow_for_the_drafts_template`: draft の template で起動し、workflow id は
  `agentic-episode-{episode_id}`。
- `test_start_reports_an_already_started_workflow_without_starting_another`: 二重起動しない。
- `test_start_for_an_episode_without_a_draft_is_episode_not_found`: 無い Episode を起動しない。
- `test_start_rejects_a_legacy_or_unrunnable_template`: 後から legacy / 未実装になった template も start で拒否。
- `test_the_temporal_starter_uses_one_workflow_id_per_episode_and_rejects_duplicates`: 実 Temporal で
  REJECT_DUPLICATE（終わった id も再利用しない）、queue は pipeline、workflow type 名。

## 7. CLI（tests/unit/test_agentic_cli.py）

- `test_plan_previews_the_compile_without_touching_the_database` / `test_plan_reports_the_refusal_code`:
  確認用の `plan --template` は DB・Temporal に触れず、拒否 code を返す。
- `test_draft_prints_the_command_id_and_resending_it_returns_the_same_episode` /
  `test_draft_with_the_same_command_id_and_another_theme_is_rejected` /
  `test_draft_without_a_command_id_generates_one`: 再送の運用（stderr の command_id）が成り立つ。
- `test_draft_with_an_invalid_input_is_a_usage_error`: UserBrief の検証違反は exit 2（DB に触れない）。
- `test_there_is_no_way_to_pass_a_budget_visibility_or_schedule`: CLI に予算・公開設定・Schedule の引数が無い。
- `test_start_is_refused_when_the_flag_is_off` / `test_learn_requires_the_flag_and_starts_the_learning_workflow`:
  flag の無い環境では起動しない（INV-55）。
- `test_a_malformed_episode_id_is_a_usage_error`: 不正な id を DB に渡さない。
- `test_plan_reads_the_pinned_plan_of_an_episode`: pin 前は `no_pinned_plan`、pin 後は計画を表示する。
