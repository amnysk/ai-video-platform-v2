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
