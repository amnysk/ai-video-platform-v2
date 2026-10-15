# ADR-0044: UI・MCP 共通のバックエンド — 操作カタログ・Application Service・/v1 API（JP-MAS-002 P2-A）

## Status

Accepted（2026-10-16）。設計 [JP-MAS-002 v1.1](../design/JP-MAS-002-v1.1.md) §2.2・§2.3・§2.5 と P2 の
バックエンド部分。前提は [ADR-0043](./0043-agentic-production-foundation.md)（Accepted、本 ADR は書き換えない）。
Web UI・MCP Server は**対象外**（境界だけ）。本番反映はしない（別承認）。flag はすべて既定 OFF のまま。

**Accepted の判断（2026-10-16、リードアーキテクト）**: 独立レビュー（Sonnet、読み取り専用）が 13 操作の単一カタログ、
service と HTTP の分離、権限（actor は認証済み principal から導出、本文に持てない）、冪等、flag OFF（認証前に 404、
workflow 起動なし）、Temporal replay（`workflows.py` 差分ゼロ）、既知 P1 の回帰テストを、コードとテストで確認した。
P0 なし。次の残リスクを承知の上で Accepted とする（いずれも後続で解消するか、上限を明記している）:

1. `/episodes/*`（Phase 1 API）が未認証で有料の再開を起動できる。**P2-B で UI を載せる前に解消する**
   （既存の smoke スクリプトを壊さないよう、設定で opt-in の認証を足す。別 ADR）。
2. `terminal_failed` の台本 job は人の復旧が要る。3. cursor は `created_at` のみ（P2-B で複合化）。
4. 実 `ApplicationService` の MCP 同等性は integration でのみ証明（unit の parity は fake 同士）。
5. `feature_disabled` の HTTP status が route により 404/409。6. `start_agentic_production` は操作者の DB 監査行が無く
   ログのみ。7. read-model は呼び出しごとに全量読み。8. 素材アップロード未実装。

## Context

実査（2026-10-15、`claude/jpmas2-p0p1` 936576d）:

- application 層の操作は `CreateEpisodeDraft`・`StartAgenticProduction`（CLI `scripts/agentic.py` だけ）と
  `ProposeStoryboardChange`（HTTP）の3つ。hold・resume・apply・読み取りは `/v1/intervention` の route が
  `InterventionBackend` を直接呼んでおり、UI・MCP が同じ操作を使うための型付きの入出力が無い。
- ADR-0043 §Consequences が残した既知のリスク3件:
  1. CreativeBrief が変わった後、成功済みの台本 job に `generate_script` が `jobs.start` を呼び
     `InvalidTransition` で止まる（`create_script_job` が既存 job を常に返す）。
  2. agentic の再開で、API（`TemporalWorkflowStarter.start_agentic_workflow`）は既定の `PipelineOptions` と
     定数の task queue を使い、介入 worker（`_choose_pipeline`）は worker 設定の `PipelineOptions` を使う。
     CLI の初回起動（`TemporalAgenticStarter`）も既定値。設定で queue・並行数を変えた環境で食い違う。
  3. 固定計画を持つ Episode を flag OFF の環境で再開すると、`AgenticEpisodeWorkflow` を拾う worker が
     無いのに workflow を投入する。
- `/episodes/*`（Phase 1 の API）は認証が無い。新しい UI 向けの操作は介入 API の principal / scope で守る。

## Decision

**UI・MCP・CLI・内部 Agent が使う操作を `contracts/application_operations.py` の `OPERATIONS` 1 か所に
（名前・効果・必要 scope・人限定・入出力 model・HTTP method/path）宣言し、`application/` の Application
Service が既存の介入 command と読み取りモデルへ委譲し、HTTP route はその薄い adapter にする。**

### 1. 操作カタログ（13 操作）

| 操作 | 効果 | scope | 人限定 | 委譲先 |
|---|---|---|---|---|
| `create_episode_draft` | propose | `propose` | – | 既存 `CreateEpisodeDraft`（生成を始めない） |
| `list_episodes` | read | `read` | – | `EpisodeReadPort.list_episodes` |
| `get_episode_workspace` | read | `read` | – | 既存 `EpisodeReadModel` + pending change set |
| `start_agentic_production` | control | `resume` | **人** | 既存 `StartAgenticProduction`（flag 必須） |
| `request_hold` | control | `hold` | – | 介入 command `request_hold` |
| `resume_production` | control | `resume` | agentic は**人** | 介入 command `resume`（legacy の AI の有料再開は既存の拒否） |
| `get_storyboard` | read | `read` | – | 現行 storyboard Artifact（`StoryboardScene` をそのまま返す） |
| `propose_storyboard_change` | propose | `propose` | – | 既存 `ProposeStoryboardChange`（適用しない） |
| `preview_change_impact` | read | `read` | – | 既存 change set の impact・見積り |
| `apply_approved_change` | apply | `apply` | – | 介入 command `apply_change`（AI の有料適用は既存の拒否） |
| `get_agent_decisions` | read | `read` | – | `decision_records` と agent run |
| `get_quality_review` | read | `read` | – | 現行 `quality_review` Artifact と `review_finding` 注記 |
| `get_workflow_progress` | read | `read` | – | Episode・job・agent run（PostgreSQL のみ） |

- 新しい scope は作らない（`granted_scope` の DB CHECK に触れない）。draft は生成も課金もしないので
  `propose`。有料の生成を始める `start_agentic_production` は `resume` に加えて `ActorType.HUMAN` を要求する
  （`forbidden`、`detail.reason=human_only`）。agentic Episode の `resume_production` も同じく人だけ
  （再開は固定計画を歩く有料の制作で、backend の「AI の有料再開」判定は開始工程が決まらない状態を覆わない）。
  legacy の resume は従来どおり backend の判定に任せる。
- 適用は**見積りの提示の後**: `apply_approved_change` は `approved_max_cost_usd` を必須とし、change set が
  未見積り（`cost_not_estimated`）・上限超過（`budget_exceeded`）・`proposed` 以外（`invalid_state`）なら
  command を作らない。最終判断は既存の apply（予算 gate・AI の有料適用禁止・期待版）が行う（二重の検査で
  緩める側は無い）。
- 絵コンテの編集範囲は ADR-0043 のまま `visual_description` だけ。構図・カメラ・参照画像・Scene 順序は
  `unsupported_change`（`StoryboardView.unsupported_fields` で UI に示す）。
- エラー code は `OperationErrorCode`（`contracts/application.py`）1 つ。新設は `invalid_state`
  （`RejectReason` と同じ綴り）・`change_set_not_found`・`cost_not_estimated`。HTTP status の写像は
  `apps/api/routers/application.py::ERROR_STATUS` 1 か所。

### 2. Application Service

- `application/service.py::ApplicationService` が 13 操作を1つずつメソッドで持つ。scope・人限定の検査は
  カタログから引いて1か所で行い、観測（§5）も1か所で記録する。HTTP・CLI・将来の MCP adapter は
  これを構築して呼ぶだけ。
- 読み取りは新しい port `EpisodeReadPort`（実装は `infrastructure/application/`）。既存の
  `EpisodeReadModel`・artifact store・`read_pinned_agentic_template` を使い、OpenSearch は読まない。
- 書き込みは既存の `CommandPort`（`receive` → `start`）。application に適用ロジックは無い（INV-50）。

### 3. 既知3問題の解消

1. **台本の作り直し**: brief を持ち TopicPlan の無い Episode だけ、`create_script_job` は「最新の台本 job が
   成功済みで、現行 brief の input_hash に一致する現行台本が無い」とき新しい `write_script` job を作る
   （Activity の再試行は新しい QUEUED job を返す）。旧い台本 Artifact・job は消さない（新しい版が現行になり、
   旧版は履歴に残る）。予約の冪等キーは input_hash から導くので新 brief は新しい予約、同じ brief の再実行は
   同じ予約（二重課金なし）。日次・TopicPlan・legacy の経路は従来どおり（`existing[0]`）。workflow のコードは
   変えない（replay 互換）。
2. **再開設定の一本化**: `PipelineOptions` を設定から作る関数を1つにし（`pipeline_options_from_settings`）、
   日次の Schedule 入力・介入 worker・API の再開・CLI の初回起動が同じ関数を使う。agentic の起動入力と
   task queue は1つの関数が作り、API と介入 worker の結果が同じことをテストで固定する。
3. **flag OFF の再開**: 固定計画を持つ Episode の再開は、API（`/episodes/{id}/resume` と
   `resume_production`）が flag OFF なら `feature_disabled` で拒否し、介入 worker も flag OFF なら
   `AgenticEpisodeWorkflow` を起動せず legacy にも落とさない（機械可読な理由を残す）。起動時に task queue の
   poller を Temporal に問い合わせ、`worker_check`（`pollers_present` / `no_pollers` / `unknown`）を返す。
   確認できなければ `unknown` で、成功（実行中）とは言わない。日次の経路は触らない。

4. **未開始の agentic draft の再開**: `CreateEpisodeDraft` が作った Episode（現行の `user_brief` はあるが
   `workflow_plan` が未 pin）は `read_pinned_agentic_template` では legacy に見える。resume が legacy の
   `EpisodePipelineWorkflow` を起動しないよう、API（`/episodes/{id}/resume` は 409、`resume_production` は
   command を作る前に `invalid_state` / `agentic_draft_not_started`）と介入 worker（`check_start` が
   `proceed=False` / `invalid_state`。flag に依存しない）が拒否する。draft を始める手段は
   `start_agentic_production` だけ。判定は `has_unstarted_agentic_draft`（metadata の行だけを見る）に1つ。

### 4. HTTP（`/v1/intervention`、`JPMAS2_APPLICATION_API` が OFF なら 404 `feature_disabled`）

route の method・path・request/response model は `OPERATIONS` から取る。既存の `/v1/intervention` route は
変えない（`/decisions` 等の形は既存 client のため維持し、新しい操作は別 path）。OpenAPI 断片
`docs/api/openapi.application.json` は生成物で、テストが生成結果との一致を検査する（TypeScript client は
これから生成できる）。CORS は設定 `API_CORS_ORIGINS`（既定は空 = middleware を付けない）。

### 5. 観測

1 操作 = 構造化ログ1行（`application.operation.completed`）。`episode_id`・`correlation_id`・`command_id`・
`actor_type`・`operation`・`workflow_id`・`outcome`・`duration_ms`・`error_code`。token・入力全文は載せない。
OpenTelemetry は導入しない（`OperationObserver` port が将来の差し込み口）。

## Alternatives

- **route ごとに use case を書く（カタログ無し）**: UI・MCP で scope・効果・path の一致を人手で保つことになる。
  INV-52 を機械検査できない。採らない。
- **新しい scope（`draft:create`・`production:start`）を足す**: `granted_scope` の DB CHECK と principal 設定の
  移行が要る。既存 scope と人限定で同じ安全性が取れる。採らない（需要が出たら別 ADR）。
- **台本 job を作り直す代わりに成功済み job を QUEUED に戻す**: Job の状態機械に後ろ向きの遷移を足すことになり、
  履歴（試行回数・成功記録）が失われる。採らない。
- **再開時の worker 確認で no_pollers なら拒否する**: poller の有無は瞬間値で、再起動中の worker を誤って
  拒否しうる。投入は Temporal が保持するので、報告に留める。
- **workflow 状態を Temporal に問い合わせて進行状況を返す**: 正本は PostgreSQL（INV-8）。採らない。

## Consequences

- 良い: 入口が増えても検証・権限・冪等・エラー code は1か所。UI・MCP は同じカタログを読む。既知3問題が
  消え、再開の設定が1つになる。
- 悪い / 負債:
  - workspace・決定・進行状況の一部は読み取りモデルの dict をそのまま返す（TypeScript では緩い型）。
    UI の要件が固まった項目から型を足す。
  - MCP tool はまだカタログから導出していない（P3。既存 15 tool は変えない）。
  - `/episodes/*`（Phase 1 API）は未認証のまま（本 ADR は flag OFF の拒否だけ足す）。
  - 素材アップロード（`asset_id`）は未実装（P2-B 以降）。
  - 独立レビュー（2026-10-15）で残した既知の制限:
    - brief を持つ Episode で、作り直しの台本 job が `terminal_failed` になると、次の再開もその job を返し
      `InvalidTransition` で止まる（legacy と同じ）。新しい job を自動で作らない: round 1 の予約キーが同じに
      なり、`spent`（生出力なし）の予約の下で生成器を呼び直しうる（予約の無い課金）。人の復旧が要る。
    - `list_episodes` の cursor は `created_at` だけ（同時刻の Episode をページ境界で取りこぼしうる）。
      `(created_at, id)` の複合 cursor は UI の要件と合わせて P2-B で足す（追加の query 引数で後方互換）。
    - `/v1/intervention/commands` から直接送った resume は、flag OFF の固定計画 Episode で hold を解いた後に
      `check_start` が `feature_disabled` で起動を止める（結果に理由は残る。hold は解けたまま）。
      `resume_production` と `/episodes/{id}/resume` は command を作る前に拒否する。
    - workspace・判断・品質・進行状況はそれぞれ `EpisodeReadModel` を全部読む（頻繁な polling には重い）。
    - flag の 404 は認証より先（flag の状態だけが分かる。既存の application route と同じ）。
