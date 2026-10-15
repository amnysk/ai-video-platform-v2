# 自律改善型制作の契約（ADR-0043 §2〜§4）

実装は P0-A（契約・Registry・Template コンパイラ・migration 0019）まで。Agent の実行基盤（Tool Gateway）は P0-C、handler は P1-A、Activity・workflow は P1。設計は [JP-MAS-002 v1.1](../design/JP-MAS-002-v1.1.md)、判断は
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

## Agent stage の実行（P1-B、INV-51 / INV-56 / INV-57）

Activity `run_agent_stage`（`workers/agents/activities.py`、task queue `agents`）。入出力は
`contracts/agentic_pipeline.py::RunAgentStageRequest` / `RunAgentStageResult`。

| 順 | 処理 | 止まり方 |
|---|---|---|
| 1 | pin された `(role, spec_version, manifest_hash)` で spec を解決 | 不一致・未知の版は**非再試行** `agent_spec_drift`（INV-51） |
| 2 | `jpmas2_agentic_workflows` と `role in enabled_agent_roles` | `blocked / agent_disabled`（黙って飛ばさない） |
| 3 | 冪等キー `agentic:{episode_id}:{stage_id}:{plan_hash}` の結果が保存済みか | あれば**何も書かず**それを返す（`replayed=true`）。提案だけ冪等に再送 |
| 4 | 入力 document の組み立て（`infrastructure/agents/inputs.py`） | 必須入力（user_brief 等）が無ければ `blocked / input_missing:<名前>` |
| 5 | 予算 gate + 予約台帳（`budget_gate.py`） | `blocked / budget_exceeded` / `budget_unverifiable` / `held`。予約が dispatch 済みで証拠が無ければ `UnreconciledReservationError`（再送しない） |
| 6 | Tool Gateway を作り handler を呼ぶ（LLM は Codex か Fake） | `AgentOutputRejected` → 8 へ。それ以外の例外は台帳を確定して送出 |
| 7 | **検証より前に**台帳を確定（`settle_agent_call`）。応答は `agent-runs/{episode}/{run}/llm-calls.json` に保存し evidence にする | LLM を試みて失敗しただけ（応答なし）なら `conservative` |
| 8 | 1 transaction で保存（`infrastructure/agents/persist.py`） | rejected は `agent_runs.outcome=rejected_output` だけ（成果物・判断・注記なし） |
| 9 | 提案を `ProposeStoryboardChange` + `SessionCommandPort` で出す | 通らなくても stage は失敗にしない（`reason=proposal_rejected:<code>`） |

### 保存（`persist_stage`、commit は呼び出し側）

- 成果物: ArtifactStore に正準 JSON を先に置き、読み戻して sha256 を照合してから `artifact_metadata` の新しい現行 revision
  にする（前の現行は降りる）。型・版は spec の `output_schema_ref` と一致し、登録済みの版でなければ `rejected_output`
- `agent_runs`: role、step = stage_id、model（LlmResult の実測）、prompt_version、tool_invocations（予約への参照つき）、
  `token_usage` / `estimated_cost_usd`（gateway の実測。不明は NULL）、snapshot の `agent_spec`（role・版・hash・policy）
- `decision_records`: actor は `agent` 種別・id = role 名（`agent:<role>`）、`agent_run_id`、`result_revision_id`。
  `target_revisions` は読んだ入力 revision、`input_snapshot_ref` は snapshot
- `revision_links(derived_from)`: 読んだ入力 revision（artifact）から出力 revision へ
- `annotations(kind=review_finding)`: 所見ごと。anchor は `scene_id` / `time_range`（と存在する manifest）。
  `payload.assessment_kind = self_assessed`
- イベント: `agent.run.recorded`（recorder）と `decision.recorded`

`may_create_revisions=false` の role（quality・learning）も**自分の出力**（`quality_review` / `lesson` Artifact）は保存する。
この旗が禁じるのは既存の制作 Artifact（storyboard 等）の revision を作ることで、変更は提案（次節）だけで出す。

### 提案は出すだけ（INV-57）

agent の `Caller` は `actor_type=agent`、scope は `read` / `comment` / `propose` だけ（`apply` なし）。
提案の要求 command_id は冪等キーと提案の添字から決まる（`proposal_request_id`）ので、replay は同じ command 群になる。
`intervention:propose` tool は提案を**積むだけ**で、保存の後にまとめて出す。ChangeSet は `proposed` のまま。

### 入力 document の表現（`inputs.py`）

| document | 中身 | 欠けたとき |
|---|---|---|
| `user_brief` `strategy_brief` `creative_brief` `storyboard` | 現行の Artifact を `parse_artifact` で読んだ JSON | `{"missing": true, "reason": ...}` |
| `script` | 現行の script Artifact（そのまま） | 同上 |
| `final_video` | `total_duration_ms` / `media` / `measured` / `technical_qa` / `render_profile` だけ（timeline・字幕 cue は渡さない） | 同上 |
| `upload_receipt` | 現行の投稿受領（そのまま） | 同上 |
| `producer_decision` | 最新の `decision_records(choose_topic)` → `{"adopted_candidate_id"}` | 同上 |
| `analytics` | 最新の `analytics_snapshots` の要約 `{measured, videos_with_views, windows, audience, snapshot_date}` | `{"measured": false, "reason": "no_analytics_snapshot"}` |
| `lessons` | 他 Episode の現行 Lesson `{"items": [{revision_id, episode_id, status, scope, observations, hypotheses}]}`（状態の解釈は handler） | `{"items": []}` |
| `recent_titles` | 他 Episode の `topic` `{"titles": [...]}` | `{"titles": []}` |
| `rule_review` | 既存の決定論 reviewer（`run_reviewer`）の `{verdict, manifest_id, findings}` | — |
| `manifest` | `{"manifest_id"}`（採用 manifest） | 同上 |
| `measurements` | `PublicMetricsPort`。本番は `{"public": false, "windows": [], "reason": "collector_not_implemented"}` | — |
| `baseline` | analytics 由来 `{available, format: "unknown", format_note, videos_with_views, windows}`。snapshot は Shorts と長尺を分けないので format は `unknown` | `{"available": false, "reason": ...}` |

欠測は**値にしない**（空 dict・0・空文字にしない）。`input_revisions` には artifact の revision だけが入り、
decision / manifest / snapshot の id は `agent_runs` の `input_refs` に残る。入力は PostgreSQL と ArtifactStore だけから読む。

### 再試行と二重課金

予約は stage ごとに 1 件（上限で計上）。LLM を呼び終えたら応答を ArtifactStore に置いて台帳を確定する。保存の前で
落ちて再試行されたら、台帳の `raw_output_key` から**保存済みの応答を再生**し LLM を呼び直さない（応答が足りなければ
`ProviderInvocationError`）。dispatch 済みで証拠が無い予約は `UnreconciledReservationError`（呼んだか不明なので再送しない）。

## Handlers（INV-57、P1-A）

`domain/agents/handlers/` に 4 つ。`HANDLERS` が `AgentHandlerRef` と1対1。純粋な domain（I/O なし）で、
入力は `AgentInput.documents[name]`（`AGENT_INPUT_NAMES` の名前だけ）、外へは `ToolGateway.llm_json` のみ。LLM に求める
JSON の形（schema）は契約 model から導く（`_support.derive_model`）。応答は契約で検証し、違反・組み立てた成果物の
違反は `AgentOutputRejected`。prompt は入力を `<data name=...>`（`<` はエスケープ）で区切り「指示ではない」と明示する。
prompt 本文は短いのでコード内に持つ（domain は file を読まない）。LLM は各 handler とも 1 回だけ呼ぶ。

| handler | 入力 document（形） | 決定論の規則 | 出力 |
|---|---|---|---|
| strategy_agent_v1 | `user_brief`（UserBrief）・`analytics`（`measured`, `videos_with_views`）・`lessons`（`items[{revision_id,status}]`）・`recent_titles`（`titles[]`） | 直近タイトルと `jaccard >= PlannerPolicy.similarity_reject` → `duplicate_of_recent_title`。profile の `eras` 外 → `era_not_in_strategy_profile`。推奨が proposed でなければ最初の proposed、無ければ拒否。`audience_basis=measured` は `measured` かつ動画 >= `MIN_VIDEOS_FOR_MEASURED_AUDIENCE`(10)、他は assumed + 不確実性行。`lessons_used`: validated=参考、他=仮説、rejected/retired は不使用 | StrategyBrief、decision `strategy_brief`（reported） |
| creative_director_v1 | `user_brief`・`strategy_brief`・`producer_decision`（`adopted_candidate_id`） | 採用候補（proposed のみ）の topic/subject/angle/era を写す。hook=`hook_options[0]`。`must_keep`→`user_constraints_applied`。profile id は user_brief | CreativeBrief、decision `creative_brief`。提案なし |
| quality_reviewer_v1 | `script`・`storyboard`（`revision_id`, `scenes[{scene_id,start_ms,duration_ms}]`, `total_duration_ms`）・`final_video`（`revision_id`, `duration_ms`）・`rule_review`（文脈のみ）・`manifest`（`manifest_id`） | `final_video`/`storyboard` 欠測 → `blocked_missing_input`（LLM なし）。存在しない scene・尺超過の所見は捨て、判断記録の不確実性に残す。critical/major があれば `needs_changes`。`self_assessed` 固定。提案: critical>major・confidence 順、scene 重複なし、最大 `MAX_PROPOSAL_EDITS`(3)、`visual_description` のみ。適用しない | QualityReview + findings + `StoryboardPatchProposal`、decision `quality_review` |
| learning_agent_v1 | `upload_receipt`（`video_id`）・`measurements`（`public`, `published_at`, `format`, `windows[]`）・`baseline`（`format`, `comparable`, `description`） | `public` が真でない・全窓に指標が無い → `awaiting_public_metrics`（artifact なし・LLM なし）。窓は入力の写し（欠測は null + `missing_reason`）。status は常に proposed。views < `SMALL_SAMPLE_VIEWS`(100) で `sample_note`。baseline が無い・比較不能・形式違いなら「比較できる基準が無い」 | Lesson（または awaiting）、decision `lesson` |

入力 document の形は呼び出し側（P1-B の入力組み立て）が合わせる。LLM が返してもよい項目だけを model に写し、
それ以外（subject, status など）は無視する。
