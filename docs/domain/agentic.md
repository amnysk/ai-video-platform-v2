# 自律改善型制作の契約（ADR-0043 §2〜§4）

実装は P0-A（契約・Registry・Template コンパイラ・migration 0019）まで。Agent の実行（handler・Tool Gateway・
Activity）と workflow は P1。設計は [JP-MAS-002 v1.1](../design/JP-MAS-002-v1.1.md)、判断は
[ADR-0043](../decisions/0043-agentic-production-foundation.md)。

## 語彙の置き場（定義は1箇所、AGENTS.md §8）

| 語彙 | 正本 |
|---|---|
| 役割名 `AgentRole`（`strategy` / `creative_director` / `quality_reviewer` / `learning` を追加） | `contracts/intervention.py` |
| 役割の責任契約 `RoleSpec` | `contracts/roles.py` |
| 版付き Agent 定義 `AgentSpec`・`ModelPolicy`・`AgentTool`・`AgentHandlerRef` | `contracts/agents.py` |
| 保存する成果物のモデル | `contracts/agentic.py` |
| `(artifact_type, schema_version)` → model | `contracts/artifact_registry.py` |
| `ArtifactType` に 6 型、`ProviderCall.AGENT_LLM` | `contracts/states.py` |
| Template・`HARD_MAX_QA_REPAIR_CYCLES` | `contracts/workflow_templates.py` |
| Template のコンパイル | `domain/workflow_templates/compiler.py` |

## Agent Registry（INV-51）

`AGENT_SPECS` は `(role, spec_version)` → `AgentSpec`。`manifest_hash` は全項目の正規 JSON（Decimal は文字列、enum は値）の
sha256。登録済み spec を変えるときは新しい `spec_version` を足す（hash は `tests/contract/test_agent_registry.py` が固定）。

共通: `model_policy_ref=codex_cli_text@1`・`max_tokens=8000`・`timeout_seconds=300`・`retry_max_attempts=3`。

| role | 入力 | 出力 | handler | tools | LLM 回数 | 上限 USD | prompt | revision を作る |
|---|---|---|---|---|---|---|---|---|
| strategy | user_brief@1.0 | strategy_brief@1.0 | strategy_agent_v1 | llm, artifact:read, analytics:read | 2 | 0.05 | strategy-v1 | はい |
| creative_director | strategy_brief@1.0 | creative_brief@1.0 | creative_director_v1 | llm, artifact:read, intervention:propose | 2 | 0.05 | creative-v1 | はい |
| quality_reviewer | storyboard@1.0 | quality_review@1.0 | quality_reviewer_v1 | llm, artifact:read, intervention:propose | 2 | 0.05 | quality-v1 | いいえ（所見） |
| learning | upload_receipt | lesson@1.0 | learning_agent_v1 | llm, artifact:read, analytics:read | 1 | 0.03 | learning-v1 | いいえ（仮説） |

`upload_receipt` は既存の成果物（`contracts/upload.py`）で版付き registry の対象外。`ModelPolicy`:
`fake_llm@1`（provider `fake`、単価 0）、`codex_cli_text@1`（provider `codex_cli`、**単価なし = 費用は測れない**。実績は
`None` で 0 にしない）。

## Artifact の型と版（INV-54）

| artifact_type | 版 | モデル | 単位 |
|---|---|---|---|
| `user_brief` | 1.0 | `UserBrief`（予算・公開設定・Schedule の項目なし） | Episode |
| `strategy_brief` | 1.0 | `StrategyBrief`（候補 1〜5、推奨は proposed の候補、`lessons_used`） | Episode |
| `creative_brief` | 1.0 | `CreativeBrief`（台本の ScriptBrief へ渡せる subject/angle/era/hook） | Episode |
| `quality_review` | 1.0 | `QualityReview`（`assessment_kind` は常に `self_assessed`） | Episode |
| `lesson` | 1.0 | `Lesson`（観測・仮説・代替説明・実験・適用範囲） | Episode |
| `workflow_plan` | 1.0 | `WorkflowPlan`（コンパイル済み計画） | Episode |
| `storyboard` | 1.0 | 既存 `StoryboardArtifact`（registry から読める） | Episode |

6 型とも `scene_id` は NULL。`ARTIFACT_MODELS`（`contracts/artifacts.py`）に型ごとに登録し、registry はその model を
版の宣言と結び付ける。`type` は他の成果物と同じく payload に持つ。

### Lesson の状態遷移

`proposed → testing | rejected | retired`、`testing → validated | rejected | retired`、`validated → retired`、
`rejected`・`retired` は終端（`LESSON_TRANSITIONS`）。`validated` だけが Strategy の「検証済みの参考」
（`LessonUse.used_as = validated_reference`）になれる。測定窓（48h / 7d / 28d）の項目が全て欠けるときは
`missing_reason` が必須で、欠測を 0 にしない。

## Workflow Template（INV-53）

| template | execution | runnable | stage | QA 修正 |
|---|---|---|---|---|
| `daily_legacy@1` | legacy_pipeline | はい（compile は `legacy_pipeline` を返す。agentic 実行は起動側が拒否。日次は Template を読まない） | script → storyboard → production → render → private_upload | 0 |
| `shorts_agentic@1` | agentic | はい | strategy → producer → creative → script → storyboard → production → render → private_upload → quality。post_publish: lesson（`public_metrics_available`） | 0 |
| `shorts_reviewed@1` | agentic | いいえ（`template_not_runnable`） | quality が private_upload の前。post_publish: lesson | 2 |

コンパイラのエラー code（`TemplateCompileError.code`）: `unknown_template`・`template_not_runnable`・`unknown_handler`・
`unknown_agent_spec`・`handler_mismatch`・`duplicate_stage`・`unknown_dependency`・`cycle`・`missing_required_stage`・
`required_stage_order`・`strategy_without_producer`・`qa_cycle_limit`・`invalid_post_publish`。出力 stage は宣言順で安定な位相順。
`plan_hash` = `plan_hash` を除いた計画の正規 JSON の sha256（`contracts.agentic.compute_plan_hash`。`WorkflowPlan` の
検証が一致を確かめる）。

## migration 0019

`artifact_metadata`（型・scene_scope）・`agent_runs.role`・`provider_reservations.provider`・
`provider_auth_incidents.provider`・`provider_rejections.provider` の CHECK を張り替えるだけ。`provider_rejections` は
ADR-0043 §4 の一覧に無いが、`models.py` が `ProviderCall` から CHECK を導くので同じ語彙に揃えた。scene_scope の
シーン単位の集合は 0014 から変わらない（新 6 型は Episode 単位）。
