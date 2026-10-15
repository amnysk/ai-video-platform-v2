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
