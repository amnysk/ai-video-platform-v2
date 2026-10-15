# JP-MAS-002 P1-B（agent worker・入力・保存）のテストの理由（ADR-0043 §6）

テストを足した理由を、足したのと同じコミットで残す。外部呼び出しなし（SQLite + InMemoryArtifactStore + 偽の
handler / LLM）。handler の本体は P1-A の担当で、ここの偽 handler は配線（入力・保存・予算・提案）だけを試す。

## 1. `run_agent_stage`（tests/unit/test_agent_stage_activity.py）

- `test_strategy_stage_writes_exactly_one_artifact_run_decision_and_a_link`: 1 回の実行が Artifact・agent_run・
  decision・revision_link を**ちょうど 1 件ずつ**同じ transaction で残し、actor・費用（codex は NULL）・token・
  agent_spec が記録されること。「成果物はあるが記録が無い」を防ぐ。
- `test_creative_stage_reads_the_adopted_decision_and_links_both_inputs`: creative が user_brief・strategy_brief の
  両方から derived_from で辿れ、producer の採用（`choose_topic`）を入力にすること。
- `test_a_replay_writes_nothing_new_and_returns_the_stored_result`: 冪等キーの 2 回目が何も書かず LLM も呼ばない
  （二重課金・二重保存の防止）。
- `test_after_a_crash_between_the_call_and_the_save_the_llm_is_not_called_again`: LLM を呼んだ後・保存の前で落ちても、
  再試行は台帳の evidence から応答を再生して LLM を呼び直さない（ADR-0013、AGENTS.md §9 の二重課金防止）。
- `test_a_changed_spec_hash_is_a_non_retryable_drift` / `test_an_unknown_pinned_spec_version_is_also_drift`: INV-51。
  計画に pin した hash と Registry が違えば、その定義を走らせず、何も書かず、retry もしない。
- `test_a_disabled_agent_is_blocked_not_silently_skipped`: flag 無効・role 無効は `blocked / agent_disabled`
  （黙って飛ばさない）。何も書かず LLM も呼ばない。
- `test_a_budget_refusal_leaves_no_reservation_and_no_artifact` / `test_an_exceeded_budget_blocks_with_budget_exceeded`:
  INV-56。換算率なし・予算超過は予約を作らず blocked。成果物も作らない。
- `test_a_held_episode_is_blocked_with_an_undispatched_reservation`: INV-44。停止中は dispatch せず（予約は未 dispatch
  のまま）blocked / held。resume 後に同じ予約を使える。
- `test_a_missing_required_input_is_blocked_before_any_reservation`: 入力が無い stage は予算も使わずに止まる。
- `test_a_rejected_output_is_recorded_without_an_artifact_and_is_not_repeated`: 契約違反の出力は `rejected_output` の
  実行記録だけを残し、成果物・判断を作らない。呼んだ事実は台帳に残り、再試行しても LLM を繰り返さない。
- `test_an_artifact_of_the_wrong_type_is_rejected_not_saved`: spec の `output_schema_ref` と違う型を保存しない。
- `test_quality_proposals_arrive_as_proposed_agent_change_sets_and_are_not_applied`: INV-57。品質の修正案は agent 主体の
  `proposed` ChangeSet として届き、現行の storyboard は変わらない。所見は scene / time_range に anchor した注記。
- `test_the_agent_caller_never_holds_the_apply_scope`: agent の scope に apply が入らない（提案だけ）。
- `test_a_replay_resubmits_proposals_idempotently`: 保存後・提案前に落ちても、replay が同じ command 群を（増やさず）出す。
- `test_an_awaiting_learning_stage_writes_no_lesson`: INV-57。private・測定欠落の stage は Lesson を作らず
  `awaiting_public_metrics`。LLM を呼んでいないので token は既知の 0 回。
- `test_unknown_token_usage_stays_null`: usage が取れない LLM は `token_usage` / 費用が NULL（0 で埋めない / INV-56）。
- `test_artifact_and_analytics_tools_are_wired_and_read_only`: `artifact:read` / `analytics:read` が配線され、何も書かない。
- `test_a_handler_cannot_use_a_tool_outside_its_spec`: INV-56。spec に無い tool は呼ぶ前に拒否され、非再試行で、
  台帳は未照合のまま残らない。
- `test_a_tool_queued_proposal_is_submitted_after_the_save_and_not_applied`: `intervention:propose` は積むだけで、保存の後に
  proposed として出る。

- `test_real_handlers_run_against_assembled_inputs_for_every_role`: P1-A の本物の `HANDLERS` + FakeAgentLlm が、組み立てた入力で
  4 role とも通る（入力 document の形の食い違いを worker 側で検出する）。

## 2. 入力の組み立て（tests/unit/test_agent_inputs.py）

- `test_every_input_name_has_a_builder` / `test_required_inputs_are_inputs_of_the_handler`: 入力名の正本
  （`AGENT_INPUT_NAMES`）と組み立てる側が食い違わない。
- `test_inputs_come_only_from_the_canonical_stores`: 正本（PostgreSQL・ArtifactStore）だけから読み、検索 index に
  依存しない。
- `test_strategy_inputs_cover_exactly_the_handler_names`: handler が読む名前だけを渡す。
- `test_without_analytics_measured_is_false_and_nothing_is_invented` / `test_analytics_summarises_the_latest_existing_snapshot`:
  分析が無いときは「測れていない」と明示し、あるときは views のある動画だけを数える（米国20代を実測と言わせない）。
- `test_lessons_of_other_episodes_come_with_their_status_and_own_lessons_do_not`: 他 Episode の Lesson を状態つきで渡し、
  自分の Episode のものは混ぜない。
- `test_recent_titles_are_other_episodes_topics`: 重複判定の材料。
- `test_producer_decision_is_the_latest_choose_topic`: 採用した候補の取り方。
- `test_missing_inputs_use_one_uniform_representation`: 欠測を空 dict に統一し（P1-A の handler は `not document` で見分ける）、理由は `missing_reasons` に残す。
- `test_quality_inputs_include_the_deterministic_review_and_the_manifest`: 既存の決定論 reviewer の所見と採用 manifest を
  モデルの所見とは別の入力として渡す。
- `test_the_default_measurements_say_the_collector_is_not_implemented` / `test_an_injected_metrics_port_supplies_the_measurements` /
  `test_no_public_metrics_never_reports_public`: 本番は収集しないので公開と言わない。mock を注入できる。

- `test_document_shapes_match_what_the_handlers_read`: storyboard の `revision_id`・`scenes`・`total_duration_ms`、baseline の
  `format/comparable/description` が handler の読む形であること。

## 3. 起動と compose

- `tests/unit/test_agent_run_worker.py::test_the_worker_exits_with_a_clear_message_when_the_flag_is_off`: INV-55。flag が
  無効なら Temporal に接続せず、理由を出して終了する。
- `tests/unit/test_agent_run_worker.py::test_the_activity_name_and_queue_come_from_the_contract`: Activity 名・queue が
  契約（`contracts/agentic_pipeline.py`）の宣言どおり。
- `tests/contract/test_agent_worker_compose.py`: `agent-worker` が `agentic` profile だけ（core に入らない）、command・
  health の queue、flag の既定が全て無効、YouTube / fal の秘密を持たない、deploy script に入らない。
- 既存テストの変更（AGENTS.md §2 の例外。ADR-0043 §6「agent worker は `agentic` profile」が先に承認済み）:
  `test_compose_workers.py::test_per_worker_minimal_env` の `CODEX_` の所有者に `agent-worker` を足し、
  `test_deploy_workers.py::test_app_services_cover_every_service_built_from_this_repo` は `agentic` 専用 service を
  「deploy 対象外」として数えず、`test_agentic_only_services_are_never_deployed_by_deploy_workers` で deploy 配列に
  入らないことを別に強制する。契約は緩めていない（core の worker は従来どおり全て deploy 配列と一致する）。
