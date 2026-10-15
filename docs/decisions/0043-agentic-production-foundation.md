# ADR-0043: 自律改善型制作の土台 — Application 層・版付き Agent Registry・版付き Workflow Template（JP-MAS-002 P0/P1）

## Status

Proposed（2026-10-15）。設計 [JP-MAS-002 v1.1](../design/JP-MAS-002-v1.1.md) の P0（境界確定と安全な拡張基盤）と
P1（Agent ワークフロー）の実装判断。前提は [ADR-0041](./0041-decision-records-and-intervention.md)・
[ADR-0042](./0042-assistant-tool-adapter.md)・[docs/domain/intervention.md](../domain/intervention.md)。
P2（Web UI）・P3（MCP Server）は**対象外**（境界だけを残す）。本番反映はしない（別承認）。

## Context

実査（2026-10-15、`claude/jpmas-integration` 9440302）:

- 変更の唯一の経路は介入 command（`infrastructure.intervention.commands.receive_command` → command workflow →
  `apply_command`）。HTTP の入口は `/v1/intervention`（`apps/api/routers/intervention.py`）で、その下に
  `apps/api/intervention_backend.py::InterventionBackend`（Protocol）がある。ユースケース層は無く、
  入口ごとの検証は route 内にある。
- 実行役の語彙は `contracts.intervention.AgentRole`（producer / writer_director / reviewer）と
  `contracts/roles.py::RoleSpec`。版（spec_version）・hash・handler・model policy・費用上限は無い。
  DB の `agent_runs.role` は migration 0016 が literal で CHECK している。
- `agent_runs.estimated_cost_usd` と `token_usage` は列があるが、どの経路も費用を書いていない。LLM（Codex）の
  予約は `estimated_cost_usd = NULL`（`budget.UNMEASURED`）。
- 実行順序を知るのは `workers/pipeline/workflows.py::EpisodePipelineWorkflow` だけ（INV-4/5）。工程の子 workflow は
  名前と task queue で起動し、`_stage_request` が入力を作る。
- 台本は `ScriptBrief`（`domain/script/brief.py`）から作る。TopicPlan がある Episode は plan の題材列、無い Episode は
  `legacy_brief(topic)`。TopicPlan は `UNIQUE(plan_date, strategy_profile_id, content_profile_id)` で、日次の
  Planner が同じ日の plan を再利用する（INV-22）。**手動・agentic の Episode が TopicPlan を作ると日次と衝突する**。
- 有料 dispatch の gate（`infrastructure.intervention.gate.check_dispatch_allowed`）は予約台帳の
  `mark_dispatched` と同じ transaction で走り、hold（INV-44）と新方式の記録必須（INV-46）を強制する。
- 本番スタック（compose project `avp2`）が同じホストで稼働中。隔離試験は `deploy/logging/test/run-e2e.sh`
  （project `avp2-oslog-c*`）。

## Decision

**入口（HTTP・CLI・アシスタント tool・内部 Agent）はすべて新設の `application/` のユースケースを通り、ユースケースは既存の
介入 command に委譲する。Agent と制作順序はそれぞれ版付きの Registry（`contracts/agents.py`）と Template
（`contracts/workflow_templates.py`）に宣言し、Episode の実行開始時にコンパイル済み計画を Artifact として固定する。
新機能はすべて Feature Flag で既定オフ、日次経路（`DailyEpisodeWorkflow` / `EpisodePipelineWorkflow`）は変更しない。**

### 1. Application 層（P0）

- 新しいトップレベル package `application/`。import してよいのは `contracts`・`domain`・自分自身だけ。
  `apps` と `workers` は `application` を import してよい。`infrastructure` は import しない（Protocol を構造的に満たす）。
  INV-6 の表を更新し、`tests/architecture/test_layering.py` の `ALLOWED` に反映する。
- `application/ports.py`: `CommandPort`（`receive(episode_id, envelopes, caller) -> list[ReceivedCommand]` と
  `start(command_id)`）。`Caller` は `actor_type`・`actor_id`・`scopes` と、port だけが解釈する不透明な `principal`。
- `application/errors.py`: `OperationError(code: OperationErrorCode, message, detail)`。
  **`OperationErrorCode` の正本は `contracts/application.py`**（HTTP status への写像は `apps/` 側で1か所）。既存の
  文字列（`invalid_payload`・`unsupported_change`・`missing_scope`・`episode_not_found`・`command_id_reused`・
  `stale_control_revision`・`stale_target_revision`・`forbidden`・`budget_exceeded`・`budget_unverifiable`）を再利用し、
  新設は `feature_disabled`・`unknown_template`・`unknown_agent_spec`・`template_not_runnable` だけ。
- 最初のユースケース `ProposeStoryboardChange`（縦断スライス）:
  - 入力 `ProposeStoryboardChangeRequest{command_id, expected_control_revision?, proposal: StoryboardPatchProposal}`、
    出力 `StoryboardProposalResult{request_command_id, commands: [{scene_id, command_id, status, created}]}`。
  - `StoryboardPatchProposal` v1.0（操作の入力なので `contracts/application.py`。要求・結果の型も同じ module）は設計 §6.1 の形。**P0 が受け付けるのは
    `visual_description` だけを変える edit**（既存 `scene.override_visual` に写る）。`framing`・`camera_movement`・
    `visual_kind`・`reference_asset_id` を含む edit は `unsupported_change`（新 ChangeKind `storyboard.patch_scenes` は
    P2 以降）。edit は 1〜12 件、scene_id の重複は `invalid_payload`。
  - edit ごとに1つの `propose_change` command。command_id は
    `uuid5(STORYBOARD_PROPOSAL_NAMESPACE, f"{request_command_id}:{index}")`。同じ要求の再送は同じ command 群
    （INV-42 で既存行）、同じ `request_command_id` で別内容なら `command_id_reused`。
  - scope `propose` を `Caller.scopes` で検査（`REQUIRED_SCOPE` から導く）。適用はしない（`apply` は別操作）。
- 入口（adapter）:
  - HTTP: `POST /v1/intervention/episodes/{episode_id}/storyboard/proposals`（`apps/api/routers/application.py`、
    認証は介入 API と同じ依存）。`JPMAS2_APPLICATION_API=false`（既定）なら `404 feature_disabled`。
  - CLI: `scripts/intervene.py propose-storyboard`（HTTP クライアント。既存と同じ token・URL 環境変数）。
  - 疑似 MCP: `contracts/assistant_tools.py` に `propose_storyboard_patch` を追加。入力 schema は
    `ProposeStoryboardChangeRequest` から**導出**し（手書きしない）、command_id は従来どおり tool 呼び出しから決める
    （INV-49）。既存 15 tool の名前・`ASSISTANT_NAMESPACE` は変えない。MCP Server（P3）はこのカタログを包むだけ。
  - 内部（Agent）: `infrastructure/intervention/internal_port.py::SessionCommandPort`（`receive_command` を自前の
    session で呼び commit、start は注入された starter）。Agent の `Caller` は `actor_type=agent`、scope は
    `read`・`comment`・`propose` だけ（apply なし）。
- 契約同期: OpenAPI の当該 path の request/response schema、`ProposeStoryboardChangeRequest.model_json_schema()`、
  アシスタント tool の入力 schema が一致することを契約テストで検査し、OpenAPI の断片を
  `docs/api/openapi.application.json` に固定（UI の TS client 生成元。生成は P2）。

### 2. 版付き Agent Registry（P0）

- `contracts.intervention.AgentRole` に `strategy`・`creative_director`・`quality_reviewer`・`learning` を足す
  （語彙の唯一の宣言元のまま。Registry は文字列を書き直さない）。`contracts/roles.py` に4つの RoleSpec を足す。
- `contracts/agents.py`:
  - `AgentTool`（StrEnum）: `llm:generate_json`・`artifact:read`・`analytics:read`・`intervention:propose`。
  - `AgentHandlerRef`（StrEnum）: `strategy_agent_v1`・`creative_director_v1`・`quality_reviewer_v1`・`learning_agent_v1`。
    **handler はコード登録だけ**（`workers/agents/handlers.py::HANDLERS`。キー集合 = `AgentHandlerRef` をテストで検査）。
    import 文字列・plugin の取得はしない。
  - `ModelPolicy{policy_id, version, provider: fake|codex_cli, model, input_usd_per_1k_tokens?, output_usd_per_1k_tokens?}`。
    単価が無い policy は**費用を測れない**（実績 `None`、0 にしない）。
  - `AgentSpec{role, spec_version, input_schema_ref, output_schema_ref, handler_ref, allowed_tools, model_policy_ref,
    prompt_version, max_llm_calls, max_tokens, max_estimated_cost_usd, timeout_seconds, retry_max_attempts, termination,
    may_create_revisions}` と `manifest_hash`（全項目の正規 JSON の sha256）。
  - `AGENT_SPECS`（`(role, spec_version)` → spec）と `resolve_agent_spec(role, version)`。未知は
    `UnknownAgentSpecError`。登録済み spec の内容変更は hash の固定テストで検出する（変えるなら新 version）。
- Tool Gateway（`domain/agents/tool_gateway.py`）: handler は gateway 経由でしか外部を呼べない。
  `allowed_tools` に無い tool・`max_llm_calls` 超過は `ToolNotAllowedError` / `AgentBudgetExceededError`。
  LLM の実体（fake / codex）は port（`domain/agents/ports.py::AgentLlm`）で注入する。
- 費用: 1 回の LLM 呼び出しの実績費用 = token usage × policy 単価。usage か単価が欠ければ `None`
  （`agent_runs.estimated_cost_usd = NULL`、`token_usage` も欠測なら NULL）。**欠測を 0 にしない**。
- 予算 gate: Agent 段の開始前に、予約台帳へ `ProviderCall.AGENT_LLM` の予約を `estimated_cost_usd = spec.max_estimated_cost_usd`
  で永続化し（INV-15）、`budget_view` + `check_additional_cost` で検査、dispatch gate（`mark_dispatched`）を通す
  （hold 中は INV-44 で止まる）。`BUDGET_USD_JPY_RATE` が未設定で上限 > 0 なら `budget_unverifiable` で止まる。
  予約の見積りは上限なので、予算計上は実績が測れなくても上限側で数える。

### 3. 版付き Workflow Template（P0 で定義・コンパイラ、P1 で実行）

- `contracts/workflow_templates.py`: `TemplateStage{id, handler, after, agent_ref?}`・`WorkflowTemplate{template_id,
  version, format_profile, runnable, stages, post_publish, limits{max_qa_repair_cycles}}`・`TEMPLATES`。
  handler の語彙 = `AgentHandlerRef` ∪ `ExistingStageHandler`（`existing_producer_adopt`・`existing_script`・
  `existing_storyboard`・`existing_production`・`existing_render`・`existing_private_upload`）。
  **Template は Python のデータとしてソース管理する**（YAML 読込・DB 登録はしない）。
- 登録: `daily_legacy@1`（記録用。コンパイルすると `legacy=True` で agentic 実行は拒否。日次は Template を読まない）、
  `shorts_agentic@1`（runnable）、`shorts_reviewed@1`（`runnable=False`。起動は `template_not_runnable`）。
- コンパイラ `domain/workflow_templates/compiler.py`（純粋関数）: 未知 handler・未知 agent_ref・handler と spec の
  `handler_ref` 不一致・id 重複・未知の `after`・循環・必須の既存工程（script→storyboard→production→render→
  private_upload の順）の欠落や順序違反・`max_qa_repair_cycles > 2`（`HARD_MAX_QA_REPAIR_CYCLES`）・post_publish に
  learning 以外を拒否。出力 `WorkflowPlan` v1.0（宣言順で安定な位相順の stage 列。agent stage は spec_version・
  manifest_hash・model_policy_ref・prompt_version を持つ）と `plan_hash`。公開設定・予算上限を表す項目は契約に無い。

### 4. Artifact Registry と永続化（P0）

- `contracts/agentic.py`（保存する成果物）: `UserBrief`・`StrategyBrief`・`CreativeBrief`・`QualityReview`・`Lesson`・
  `WorkflowPlan`（全て `schema_version: "1.0"`、`extra="forbid"`）。
- `contracts/artifact_registry.py`: `(artifact_type, schema_version)` → model。`parse_artifact` は未知の版を
  `UnsupportedArtifactSchemaError` で拒否（黙って読み替えない）。upcast は明示登録した関数だけ。既存
  `storyboard@1.0` も登録する。
- `ArtifactType` に `user_brief`・`strategy_brief`・`creative_brief`・`quality_review`・`lesson`・`workflow_plan`
  （Episode 単位、`scene_id` は NULL）。`ProviderCall` に `agent_llm`（Episode 単位）。
- migration **0019**: `ck_artifact_metadata_type`・`ck_artifact_metadata_scene_scope`・`ck_agent_runs_role`・
  `ck_provider_reservations_provider`・`ck_provider_auth_incidents_provider` の張り替えだけ（新しい表・列は作らない）。
  語彙は literal で凍結。downgrade は新語彙の行があれば何も変えずに拒否。既存 migration は編集しない。
- 内容と版は既存 `artifact_metadata` + MinIO、依存は `revision_links(derived_from)`、判断は `decision_records`、
  実行は `agent_runs`。Agent 専用の表は作らない（設計 §8）。

### 5. Feature Flag（P0）

`infrastructure/config.py::Settings`（既定はすべて無効）:

| env | 既定 | 効果 |
|---|---|---|
| `JPMAS2_APPLICATION_API` | `false` | §1 の HTTP route。無効なら `404 feature_disabled` |
| `JPMAS2_AGENTIC_WORKFLOWS` | `false` | agentic workflow / agent activity の登録、`StartAgenticProduction` |
| `JPMAS2_ENABLED_AGENTS` | 空 | 有効な agent role（カンマ区切り）。無効な role の stage は `agent_disabled` で止まる（黙って飛ばさない） |

日次経路（`DailyEpisodeWorkflow`・`EpisodePipelineWorkflow`・Topic Planner）はこれらの flag も Registry も Template も
参照しない。

### 6. Agent ワークフロー（P1）

- 新 workflow `AgenticEpisodeWorkflow`（`workers/pipeline/agentic_workflow.py`、task queue は pipeline と同じ）。
  `EpisodePipelineWorkflow` は**書き換えない**（replay 互換）。既存工程は同じ子 workflow・同じ id 規則・同じ
  `STAGE_PARKING_STATUS`・同じ control gate / upload gate Activity を使う。
- 開始時に Activity `pin_workflow_plan` が Template をコンパイルし `workflow_plan` Artifact として保存する。その
  Episode に計画が既にあれば**それを返す**（Registry が変わっても実行中の計画は変わらない / INV-51）。workflow は
  Activity の結果（履歴）にある計画だけを使う。
- Agent stage は Activity `run_agent_stage`（task queue `agents`、`workers/agents/`）。pin された
  `(role, spec_version, manifest_hash)` で spec を解決し、hash が違えば非再試行の `agent_spec_drift`。flag・予算 gate・
  Tool Gateway を通し、出力を契約で検証し、Artifact・`agent_runs`（費用は §2）・`decision_records`・`revision_links` を
  **同じ commit** に入れる。冪等キーは `agentic:{episode_id}:{stage_id}:{plan_hash}`。
- `shorts_agentic@1` の順序: `strategy` → `producer`（`existing_producer_adopt`: StrategyBrief の候補を規則で採用し
  `decision_records(choose_topic)` と Episode の topic を書く。**TopicPlan は作らない**）→ `creative`（CreativeBrief：題材・
  角度・時代・フック）→ script → storyboard → production → render → private_upload → `quality`（QualityReview と
  `review_finding` 注記、修正は `ProposeStoryboardChange` の**提案だけ**）。
- 台本は CreativeBrief を使う: `workers/planning` の brief 決定に「TopicPlan が無く、現行の `creative_brief` Artifact が
  ある Episode」の分岐を足す。plan のある日次・どちらも無い legacy は従来どおり。
- QA: 自動適用なし（有料の自動承認は初期オフ）。修正サイクルは `limits.max_qa_repair_cycles`（上限 2）で数え、
  workflow は後ろ向きの辺を持たない。
- Learning: 別 workflow `AgenticLearningWorkflow`（post_publish、明示起動）。投稿受領が private のまま、または
  測定が欠ける・小標本なら `awaiting_public_metrics` を返し Lesson を作らない。測定があれば `Lesson`（`status=proposed`）。
  `validated` 以外の Lesson は Strategy の入力で「仮説」として区別する。
- `CreateEpisodeDraft`（Episode・UserBrief・`episode_controls(mode=auto_intervenable)` を作るだけ。生成を始めない。
  日次の枠・TopicPlan を使わない）と `StartAgenticProduction`（flag 必須）は所有者の CLI `scripts/agentic.py` から呼ぶ。
  HTTP 化は P2。
- agent worker は compose の **`agentic` profile**（`core` には入れない。既定の `make up` では起動しない）。

### 7. 不変条件（番号は `docs/invariants.md` の空き 50〜57、テストと同じコミットで追加）

| ID | 内容（要旨） |
|---|---|
| INV-50 | 単一変更経路: StoryboardPatchProposal から propose_change を作るのは `ProposeStoryboardChange` だけ。HTTP・アシスタント・内部 Agent で同じ結果型・同じ拒否 code |
| INV-51 | 定義の固定: 実行開始後の計画（template・agent spec の版と hash）は変わらない。未知の版は拒否、hash 不一致は止まる |
| INV-52 | 契約同期: OpenAPI・アシスタント tool schema・contracts の model が一致 |
| INV-53 | Template の安全性: 未登録 handler/agent、循環、必須工程の欠落、QA 上限超過、未実装 Template を起動しない |
| INV-54 | 保存の互換: 未知の schema 版は読まない（失敗安全）、旧 Storyboard v1.0 は読める、migration は追加だけ |
| INV-55 | 既定オフ: 新機能の flag は既定で無効、日次経路は agentic のモジュールを参照しない |
| INV-56 | Agent は Tool Gateway が許した tool だけを上限内で呼び、LLM 費用の欠測を 0 にせず、予約・予算 gate を通ってから呼ぶ |
| INV-57 | QA・Learning は提案・仮説まで: 自動適用せず、private の動画・欠測を低評価と断定しない |

## Alternatives

- **既存 route に直接ユースケースを足す**（application 層を作らない）: UI・MCP・Agent が増えるたびに検証を複製する。
  設計 §2.2 が否定。採らない。
- **`EpisodePipelineWorkflow` をテンプレートエンジンに書き換える**: 稼働中の日次の履歴の replay が壊れる（patch が
  必要で、誤ると本番の日次が止まる）。設計 §2.6 が否定。新 workflow にした。
- **Template を YAML / DB に置く**: パーサ・登録 API・権限が必要で、任意入力からの実行経路を作りうる。初期は
  ソース管理された Python データ（レビュー必須）にした。外部化は需要が出たら別 ADR。
- **Agent の producer 採用で TopicPlan を作る**: `uq_topic_plans_day_profile` と INV-22 で日次 Planner と衝突し、
  日次が手動の plan を再利用しうる。採らない（Episode の topic と判断記録だけ）。
- **Agent 専用の表（strategy_briefs 等）**: Agent の数だけ migration が増える。設計 §8 が Artifact 優先を指示。採らない。
- **agent LLM を予約台帳に載せない**: INV-15・hold（INV-44）・予算計上を迂回する。採らない。

## Consequences

- 良い: 入口が増えても検証・冪等・権限・拒否 code は1か所。Agent/Template の追加は Registry への登録とテストで済み、
  実行中の Episode は固定計画で動く。日次は flag・コードとも無関係のまま。
- 悪い / 負債:
  - `storyboard.patch_scenes`（構図・カメラ・参照画像）は未実装。P0 は映像文の差し替えだけを受ける。
  - Quality Reviewer は P1 では代表フレームの画像を VLM に渡さない（テキスト入力 + technical_qa + 決定論 reviewer
    の所見）。フレーム抽出は P4。
  - Codex CLI の model policy は単価が無く、実績費用は測れない（`None`）。予算は予約の上限で数える。
  - 解析（48h/7d/28d）の新しい収集は無い。Learning は既存 `analytics_snapshots` と、隔離試験の mock を読む。
  - OpenTelemetry・React Flow・Konva は導入しない。境界（application port・Artifact 契約・OpenAPI 断片）だけを残す。
  - MCP Server・OAuth・Web UI は未実装（P2/P3）。
