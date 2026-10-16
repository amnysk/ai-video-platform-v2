# Application 層（ADR-0043 §1）

入口（HTTP・CLI・アシスタント tool・内部 Agent）が共通に通る操作の層。決定の理由は
[ADR-0043](../decisions/0043-agentic-production-foundation.md)、不変条件は INV-50（単一変更経路）・INV-52（契約同期）、
依存の向きは INV-6。

## 1. 層と port

- `application/`: import してよいのは `contracts` と `domain` だけ。`apps` と `workers` は import してよい。
  `infrastructure` は import しない（port を構造的に満たし、`contracts.application.OperationError` を投げる）。
- `application/ports.py`: `Caller(actor_type, actor_id, scopes, principal)`（`principal` は port だけが解釈する不透明な値）、
  `ReceivedCommand(command_id, status, created, change_set_id)`、`CommandPort`（`receive(episode_id, envelopes, caller)` と
  `start(command_ids)`）。
- 実装: HTTP は `apps/api/application_adapters.py::BackendCommandPort`（既存 `InterventionBackend` を包む）、内部（Agent）は
  `infrastructure/intervention/internal_port.py::SessionCommandPort`（自前の session で `receive_command` を1 transaction で呼ぶ。
  主体は `agent` / `ai_assistant` だけ。scope は呼び出し側が渡す）。

## 2. 契約（`contracts/application.py`）

- `OperationErrorCode`: 既存の綴りを再利用（`invalid_payload` `unsupported_change` `missing_scope` `episode_not_found`
  `command_id_reused` `stale_control_revision` `stale_target_revision` `forbidden` `budget_exceeded` `budget_unverifiable`。
  `RejectReason` と同じ綴りであることをテストで検査）。新設は `feature_disabled` `unknown_template` `unknown_agent_spec`
  `template_not_runnable`。HTTP status への写像は `apps/api/routers/application.py::ERROR_STATUS` の1か所
  （409: command_id_reused / stale_*、403: missing_scope / forbidden、404: episode_not_found / feature_disabled、他は 422）。
- `OperationError(code, message, detail)`: 例外型も contracts に置く（infrastructure の port が投げるため。`application.errors` は再 export）。
- `StoryboardPatchProposal` v1.0（設計 §6.1）・`ProposeStoryboardChangeRequest`・`StoryboardProposalResult`・
  `storyboard_edit_command_id`（`uuid5(STORYBOARD_PROPOSAL_NAMESPACE, "{request_command_id}:{index}")`）。
  `storyboard_request_sha256`（要求全体の指紋。全 command の `ProposeChangePayload.request_sha256` に載せ、件数違いの再送も
  `command_id_reused` にする。任意 field の追加で後方互換・migration なし）。
  `visual_description` の長さは `SceneOverrideVisual` の制約から導出する。

## 3. ユースケース `ProposeStoryboardChange`

1. scope `propose`（`REQUIRED_SCOPE[propose_change]`）が無ければ `missing_scope`。
2. path の Episode と `proposal.episode_id` が違えば `invalid_payload`。scene_id の重複も `invalid_payload`。
3. `framing` `camera_movement` `visual_kind` `reference_asset_id` を含む edit、`visual_description` の無い edit は
   `unsupported_change`（`detail.fields` に項目名。`storyboard.patch_scenes` は未実装 = P2 以降）。
4. edit ごとに `propose_change` の envelope（`SceneOverrideVisual` + `ProposeChangePayload` を本物の model で検証）を作り、
   `port.receive` を1回、作られた command だけ `port.start`。適用はしない。
5. 戻り値は `StoryboardProposalResult`。HTTP は新規があれば 202、全て再送なら 200。

## 4. 入口

| 入口 | 場所 |
|---|---|
| HTTP | `POST /v1/intervention/episodes/{episode_id}/storyboard/proposals`（`apps/api/routers/application.py`。認証は介入 API と同じ `current_principal`） |
| CLI | `scripts/intervene.py propose-storyboard --episode ID --file proposal.json [--command-id ID] [--expected-control-revision N]` |
| アシスタント | tool `propose_storyboard_patch`（入力 schema は `StoryboardPatchProposal` から導出 + `expected_control_revision`。request の command_id は `tool_command_id(session, tool_call)`）。上の HTTP route を呼ぶ |
| 内部 Agent | `ProposeStoryboardChange(SessionCommandPort(session_factory, starter))` |

## 5. Feature flag

`JPMAS2_APPLICATION_API`（`Settings.jpmas2_application_api`、既定 `false`）。false の間は route が `404 feature_disabled`
（認証・本文の検査より先）。flag のあるものは HTTP route だけで、内部 port・CLI・tool は route を経由する／use case を呼ぶだけ。

## 6. grep の記録（AGENTS.md §8、変更前）

`grep -rn "jpmas2_application_api\|JPMAS2_APPLICATION_API" apps/ workers/ domain/ infrastructure/ contracts/ docs/`: 変更前は
ADR-0043 と設計の記述（読み手でも書き手でもない）のみ。コード上の読み手は `apps/api/routers/application.py` の1件、
書き手は `infrastructure/config.py` の宣言1件。

## 7. OpenAPI 断片

`docs/api/openapi.application.json` は application 層の path とそれが参照する schema だけ。
`python scripts/export-openapi.py --application` で再生成する（UI の TypeScript client の生成元。生成は P2）。

## 8. ユースケース `CreateEpisodeDraft` / `StartAgenticProduction`（ADR-0043 §6、P1-C）

所有者の CLI（`scripts/agentic.py`）が呼ぶ。HTTP 化は P2。`application/use_cases/agentic_production.py`。

- 型は `contracts/application.py`: `CreateEpisodeDraftRequest{command_id, brief: UserBrief}`・`EpisodeDraftResult`・
  `StartAgenticProductionRequest{episode_id}`・`StartAgenticProductionResult{episode_id, workflow_id, started}`・
  port の結果型 `DraftRecord`・`StartedWorkflow`。`episode_draft_id(command_id)` = `uuid5(EPISODE_DRAFT_NAMESPACE, command_id)`。
- port（`application/ports.py`）: `EpisodeDraftPort`（`create_draft`・`get_brief`）、`AgenticWorkflowStarter`（`start`）。
  実装は `infrastructure/agents/draft_port.py::DbEpisodeDraftPort` と
  `infrastructure/temporal/agentic_starter.py::TemporalAgenticStarter`。
- `CreateEpisodeDraft`: template を `compile_agentic_template` で確かめる（未知 `unknown_template`、`runnable=False` と
  `daily_legacy` は `template_not_runnable`、`format_profile` 不一致は `invalid_payload`）。Episode（topic = theme、
  `planned`、TopicPlan なし、日次の枠なし）・UserBrief Artifact・`episode_controls(mode=auto_intervenable)` を**1 commit**で作る。
  Episode id が `command_id` から決まるので、同じ id・同じ内容は同じ Episode（`created=false`）、別内容は
  `command_id_reused`。生成は始めない（workflow を起動する依存を持たない）。
- `StartAgenticProduction`: `jpmas2_agentic_workflows` が無効なら `feature_disabled`（flag は構築時に渡す）。
  UserBrief の無い Episode は `episode_not_found`。template を draft と同じ検査にかけてから
  `AgenticEpisodeWorkflow`（id `agentic-episode-{episode_id}`、REJECT_DUPLICATE）を起動する。既にあれば `started=false`。
  再開（介入 command の resume・`POST /episodes/{id}/resume`）は同じ id を `ALLOW_DUPLICATE` で再起動する（[intervention.md](./intervention.md) の「agentic Episode の resume」）。

grep の記録（AGENTS.md §8、変更前）: `grep -rn "agentic-episode\|agentic_episode_workflow_id" apps/ workers/ domain/
infrastructure/ contracts/ docs/` は `contracts/agentic_pipeline.py` の宣言1件のみ（読み手・書き手は本変更で追加）。

