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

## Agent 実行基盤（INV-55 / INV-56、P0-C）

handler の契約（実装は P1）と、handler が外へ出るための唯一の道。

| 部品 | 場所 | 役割 |
|---|---|---|
| `AgentLlm` / `LlmResult` | `domain/agents/ports.py` | LLM の port。token が取れなければ `None` |
| `llm_call_cost` / `sum_costs` | `domain/agents/cost.py` | 費用 = usage × 単価。不明は `None`（0 にしない） |
| `ToolGateway` | `domain/agents/tool_gateway.py` | `allowed_tools` 外・未供給の tool と `max_llm_calls` 超過を呼ぶ前に拒否。呼び出しを記録（prompt 本文なし） |
| `AgentInput` / `AgentOutput` / `AgentHandler` / `AGENT_INPUT_NAMES` | `domain/agents/runtime.py` | handler の入出力。入力 document 名の正本 |
| `CodexAgentLlm` / `FakeAgentLlm` | `infrastructure/agents/` | 実装。Codex は `StoryGenerator` を注入で受ける（codex_cli を直接 import しない / INV-18） |
| `reserve_and_dispatch_agent_call` / `settle_agent_call` | `infrastructure/agents/budget_gate.py` | `ProviderCall.AGENT_LLM` の予約（上限で計上）→ 予算検査 → dispatch gate |

- 例外: `ToolNotAllowedError`・`AgentBudgetExceededError` は `PermanentError`（retry しない）、
  `AgentBudgetRefusedError`（`reason` = `budget_exceeded` / `budget_unverifiable`）は `NeedsInputError`。
- `RunCapture` は任意の `estimated_cost_usd`（不明は NULL のまま）と `agent_spec`（role・spec_version・
  manifest_hash・model_policy_ref。snapshot に残る）を持つ。従来の経路は None のままで何も変わらない。
- gateway の `token_usage()` / `actual_cost_usd()` は LLM を 1 回も呼ばなければ既知の 0、1 回でも usage・単価が
  欠ければ `None`（失敗した呼び出しも「不明」として数える）。
- Flag と運用は [docs/operations/agentic.md](../operations/agentic.md)。

## AgenticEpisodeWorkflow と計画の固定（ADR-0043 §6、P1-C）

`workers/pipeline/agentic_workflow.py`。名前・queue・Activity 名・入出力は `contracts/agentic_pipeline.py`。
`EpisodePipelineWorkflow` は書き換えない（replay 互換）。flag `JPMAS2_AGENTIC_WORKFLOWS` が有効な pipeline worker
だけが登録する（`workers/pipeline/run_worker.py::registrations`）。

1. `pin_workflow_plan`（pipeline worker）: Episode に現行の `workflow_plan` があればそのまま返す（Template・Registry が
   変わっていても。INV-51）。無ければ `compile_agentic_template` でコンパイルし、Artifact と判断記録
   （`decision_type=pin_workflow_plan`、`decision.recorded`）を同じ commit に書く。`daily_legacy`・`runnable=False`・未知は
   非再試行の `ApplicationError`（`type` = `OperationErrorCode` の値）。結果に stage ごとの `retry_max_attempts`
   （pin した spec から引いた値）を載せ、workflow は Registry を読まない。
2. 計画の stage を順に: control gate（`PIPELINE_CONTROL_GATE`。agent stage・producer は待機地点を持たない先頭工程
   `NON_PIPELINE_GATE_STAGE` で問うので停止中かどうかだけが効く）→ 停止中・待機中なら `parked`。
3. agent stage は `run_agent_stage`（queue `agents`、timeout は計画、retry 回数は pin した spec、`agent_spec_drift` は非再試行）。
   `blocked` / `rejected_output` / Activity の失敗は理由を残して `stopped`。黙って飛ばさない。
4. `existing_producer_adopt` は `adopt_strategy_candidate`（規則 `RationaleKind.RULE`）。現行 StrategyBrief の推奨候補を
   採用し、`decision_records(choose_topic, actor=agent:producer)` と Episode の topic を書く。TopicPlan は作らない。
5. `existing_script/storyboard/production/render/private_upload` は既存と同じ子 workflow・id・queue・入力
   （`_stage_request`）・`STAGE_PARKING_STATUS`。private_upload の前に upload gate。
6. `quality` は計画に1度しか無く1度だけ走る（後ろ向きの辺が無い）。QA の提案数は `qa_repair_cycles` に数えるだけで
   自動適用しない。
7. `AgenticLearningWorkflow`（`scripts/agentic.py learn` が明示起動）: `load_workflow_plan` で pin 済みの計画を読み、
   post_publish の learning stage を `run_agent_stage` へ渡す。新しい計画は pin しない。`awaiting_public_metrics` は言い換えない。

### 台本の brief（`workers/planning/activities.py::ScriptActivities._brief`）

TopicPlan がある Episode は plan、**TopicPlan が無く現行の `creative_brief` がある Episode は CreativeBrief**
（`domain/script/brief.py::creative_brief`。検証は `plan_brief` と同じ、`topic_plan_id` は None）、どちらも無ければ legacy。
INV-55 のため `workers/planning` が読む agentic の語彙は `CreativeBrief` と `contracts.artifact_registry` だけ。

### draft / start / learn

`scripts/agentic.py draft | start | learn | plan`（所有者向け、DB と Temporal に直接つなぐ）。使い方は
[docs/operations/agentic.md](../operations/agentic.md)、use case は [application.md](./application.md) §8。
`draft` は生成を始めない。`start` は flag 必須で、`agentic-episode-{episode_id}` を REJECT_DUPLICATE で起動する
（同じ Episode の再起動は `started=false`。**駐機（hold・待機）後の再開は介入 command が従来の
`EpisodePipelineWorkflow` を start する**ので、agentic の stage（quality 等）は再開の経路では走らない。P2 で設計する）。

