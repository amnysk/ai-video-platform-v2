# JP-MAS-002 P0-A（契約・Registry・Template コンパイラ・migration 0019）のテストの理由（ADR-0043）

テストを足した理由を、足したのと同じコミットで残す。

## 1. 成果物の契約（tests/contract/test_agentic_contracts.py）

- `test_user_brief_has_no_budget_visibility_or_schedule_field`: 人の依頼から予算・公開設定・Schedule を変えられない
  （INV-47、AGENTS.md §9）。余分な項目も `extra="forbid"` で拒否する。
- `test_user_brief_defaults_and_limits`: 既定（en-US・米国20代・shorts_en・既定 profile）と各長さ上限。profile の既定は
  `contracts/topic_planning` の定数と一致させる（再掲しない）。
- `test_strategy_brief_round_trips` / `test_strategy_candidates_are_one_to_five_with_unique_ids` /
  `test_the_recommendation_must_be_a_proposed_candidate` / `test_rejected_reason_is_required_iff_rejected`:
  Strategy の出力が producer の採用規則へ渡せる最小の整合（却下候補を推奨できない・理由なしの却下を許さない）。
- `test_only_a_validated_lesson_is_a_validated_reference`: validated 以外の Lesson を「検証済みの参考」と呼ばせない（設計 §4）。
- `test_creative_brief_fields_are_compatible_with_topic_plan_limits`: CreativeBrief が台本の ScriptBrief（slug・長さ）へそのまま渡せる。
- `test_quality_review_is_always_self_assessed`: 自己評価を視聴者データとして扱わない（設計 §3）。
- `test_quality_finding_limits` / `test_quality_review_consistency`: 確信度・時間範囲・重大所見のある review を pass にしない・
  入力欠落の blocked に欠けた入力を書かせる。
- `test_lesson_transitions_are_exactly_the_documented_table`: 状態遷移表の辺を増減させない（終端の rejected/retired）。
- `test_a_measurement_window_without_metrics_needs_a_reason`: 欠測を黙って 0/空にしない。0 は測定できた値として許す。
- `test_lesson_windows_are_unique_and_times_are_timezone_aware`: 同じ窓の重複・時刻の tz 不明を拒否。
- `test_every_new_model_is_frozen_strict_and_versioned`: 全モデルが `extra=forbid`・frozen・`schema_version` を Literal で持つ。
- `test_new_types_are_registered_episode_scoped_models`: `ARTIFACT_MODELS` 登録と、6 型がシーン単位でないこと。

## 2. Agent Registry（tests/contract/test_agent_registry.py）

- `test_registered_spec_hashes_are_pinned`: 登録済み spec の内容変更を検出する（INV-51。直すなら新 spec_version）。
- `test_changing_any_field_changes_the_hash`: hash が項目に感応している（固定テストが空振りしない）。
- `test_every_new_role_has_a_v1_spec_and_a_role_spec` / `test_unknown_role_or_version_is_rejected`: 4 役の登録と未知の拒否。
- `test_tools_handlers_and_policies_are_registered_vocabulary`: tool・handler・policy が登録語彙内で、handler 集合は
  `AgentHandlerRef` と一致する（P1 の `HANDLERS` キー検査の前提）。
- `test_role_spec_tools_equal_the_agent_spec_tools`: RoleSpec と AgentSpec の二重宣言が食い違わない（§8）。
- `test_schema_refs_resolve_in_the_artifact_registry`: spec の入出力参照が実在する版を指す（`upload_receipt` は版なしの既存型）。
- `test_model_policies_never_invent_a_price`: 単価の無い policy を 0 円として扱わない。

## 3. Artifact Registry（tests/contract/test_artifact_registry.py）

- `test_the_registry_holds_the_adr_0043_entries_and_the_adr_0047_fact_check_report`: 登録の過不足と、本番 upcaster が空であること。
- `test_a_stored_storyboard_v1_is_readable_through_the_registry`: 旧 storyboard v1.0 を読める（INV-54）。
- `test_a_new_artifact_round_trips`: 新型が registry 経由で読める。
- `test_an_unknown_schema_version_is_rejected_not_reinterpreted` / `test_a_missing_or_non_string_version_is_rejected` /
  `test_an_unregistered_type_is_rejected`: 未知の版・欠落・未登録型を推測で読まない。
- `test_reading_across_versions_needs_an_explicitly_registered_upcaster`: upcast の仕組みを test 専用 registry で証明する。
  登録が無ければ（同じ registry に v2 があっても）読み替えない。逆向きも不可。

## 4. Template の契約とコンパイラ（tests/contract/test_workflow_templates.py、tests/unit/test_workflow_template_compiler.py）

- `test_no_template_or_plan_field_can_express_visibility_budget_or_schedule`: Template から公開設定・予算を拡張できない。
- `test_the_registered_templates_are_exactly_the_three_adr_templates` / `test_shorts_agentic_has_exactly_the_adr_stage_sequence` /
  `test_shorts_reviewed_reviews_before_the_private_upload` / `test_template_handlers_come_from_the_two_registered_vocabularies` /
  `test_template_hash_is_stable_and_sensitive`: ADR §3 の登録内容と hash。
- `test_every_registered_template_is_structurally_valid_and_compiles_iff_runnable`: 全 Template が構造検査を通り、
  runnable だけがコンパイルできる。
- `test_daily_legacy_compiles_to_the_legacy_pipeline`: 日次は `legacy_pipeline` を返し agentic 実行の入口が拒否できる。
- `test_the_agentic_plan_pins_every_agent_spec_version_and_hash` / `test_compilation_is_deterministic_and_the_hash_covers_the_agent_specs` /
  `test_a_tampered_plan_is_rejected_by_its_own_hash`: 計画が spec の版・hash を固定し、改ざんは検出される（INV-51）。
- `test_output_order_is_topological_with_declaration_order_for_ties` / `test_ties_keep_declaration_order`: 出力順が決定的。
- 以降の `test_unknown_template` … `test_invalid_post_publish`: エラー code ごとに 1 つの変異（INV-53）。
  `test_a_required_stage_appearing_twice_is_missing_required_stage` は「ちょうど 1 回」の裏側。

## 5. migration 0019（tests/contract/test_migration_0019_agentic_vocabulary.py）

- `test_pre_0019_literals_are_the_values_at_9440302` / `test_frozen_literals_equal_the_current_enums_in_order`: downgrade 先の
  凍結値が履歴どおりで、upgrade 先が今の enum と一致する（値だけ足して migration を書かない事故を止める）。
- `test_0019_adds_exactly_the_adr_0043_values` / `test_downgrade_restores_every_upgraded_check` /
  `test_the_scene_scope_is_unchanged_so_new_artifact_types_are_episode_scoped`: 足す語彙の過不足と、張り替える制約の対応。
- `test_the_migration_does_not_import_contracts` / `test_the_migration_creates_no_table_or_column`: 凍結と「制約だけ」。
- `test_upgrade_and_downgrade_round_trip_on_sqlite`: 制約が変わり、artifact_metadata の式索引を失わない。
- `test_downgrade_refuses_and_changes_nothing_when_a_new_value_is_in_use`: 4 表それぞれで、新語彙の行があれば拒否し、
  制約・version・行が変わらない。`test_downgrade_succeeds_when_only_old_vocabulary_rows_exist`: 旧語彙の行だけなら戻せる。

## 6. 既存テストの変更（AGENTS.md §2）

- `tests/contract/test_migration_0016_intervention.py::test_frozen_vocabulary_matches_the_contracts`: 0016 が凍結した role は
  3 つ。ADR-0043 で `AgentRole` が 4 つ増えたので、`later` の除外集合に足した（0017 の RESOLVE_ANNOTATION と同じ先例。
  新しい 4 つは 0019 のテストが検査する）。
- `tests/contract/test_migration_frozen_vocabulary.py::test_0014_upgrade_adds_exactly_the_adr_0035_values`: 同じ理由で、
  0019 が足した artifact 6 型と `agent_llm` を比較から除外した（0014 の意味は変わっていない）。
