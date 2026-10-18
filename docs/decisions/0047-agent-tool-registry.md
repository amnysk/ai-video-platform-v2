# ADR-0047: Agent Tool Registry — Research / YouTube Analytics を Agent の tool として統合する

## Status

Proposed（2026-10-18）。前提は [ADR-0043](./0043-agentic-production-foundation.md)（Agent Registry / Tool Gateway）・
[ADR-0036〜0039](./0036-research-search-provider-adapters.md)（Research）・[ADR-0025](./0025-topic-planner.md)
（YouTube Analytics adapter）・[ADR-0041](./0041-decision-records-and-intervention.md)（判断記録と介入）。
**実装の基点は `claude/jpmas2-p0p1`（PR #5、未マージ）の先端。** 既定ブランチ `claude/episode-vertical-slice` には
Agent Runtime が無いので、作業ブランチ `claude/agent-tools` は jpmas2-p0p1 から切る。既定ブランチへのマージ・本番反映は
この ADR の対象外（別承認）。

## Context

実査（2026-10-18、`claude/jpmas2-p0p1` 9870c06）:

- 既に `domain/agents/tool_gateway.py::ToolGateway` があり、`AgentSpec.allowed_tools`（`AgentTool` enum 4 値）を検査し、
  `agent_runs.tool_invocations` に記録する。**ただし tool に schema・版・timeout・冪等キー・tool_call_id・引数/結果の
  記録・費用は無く**、現行の 4 handler は `gateway.call` を 1 度も呼ばない（事前取得した `documents` を渡して LLM を 1 回呼ぶ型）。
- Agent は 1 stage = 1 Activity（`run_agent_stage`）で動き、LLM 応答は MinIO に保存して再試行では `ReplayLlm` が再生する。
  workflow は決定的で、Registry を読まず、Activity の結果（履歴）にある計画だけを使う（INV-51）。
- `AgentSpec.manifest_hash` は `tests/contract/test_agent_registry.py` が literal で固定している（INV-51）。
- Research は `ResearchGateway.submit` → Temporal `ResearchWorkflow` → DB ポーリング → MinIO 読み戻しの**非同期**経路。
  冪等性（`idempotency_key` / `request_hash` / 呼び出し単位の台帳）は完備。**実 Provider は未配線**（`RESEARCH_PROVIDER` は
  `fake` / `none`）。任意 URL を取得する公開入口は無い。Agent ID・Tool Call ID の保存先も無い。
- YouTube Analytics adapter は窓（7/28/90 日）単位の合計とチャンネル視聴者構成のみ。動画別の `engagedViews`・`comments`・
  維持率・公開日・尺・Shorts/長尺の区別は無い。quota 超過・閾値による欠落・反映遅延を区別する分類が無い。
- OpenTelemetry / Prometheus はコードに無い（README が未実装と明記）。構造化ログ → Fluent Bit → OpenSearch は実在（ADR-0040）。
- 評価した OSS（PyPI 2026-10-18 確認）: pydantic-ai-slim 2.57.0、fastmcp 4.1.1、langgraph 1.2.16、openai-agents 0.24.0、
  langchain 1.4.6。YouTube Analytics MCP 4 件（gomcpgo / conorbronsdon / pauling-ai / modbender）は全て別プロセスで作成から
  2 か月未満（pauling-ai のみ約 8 か月）、star 0〜28、うち 1 件は書き込み tool を含む。

## Decision

**既存の `ToolGateway` に、コード登録の軽量な Tool Registry（`ToolDef`）を足す。新しい依存は入れない。Pydantic AI・
FastMCP・LangGraph・OpenAI Agents SDK・LangChain・既存の Analytics MCP は採用しない。MCP は公開が必要になった時点で、
この Registry の上の薄い adapter として別 ADR で足す。**

### 1. 契約（`contracts/agent_tools.py`、唯一の宣言元）

- `ToolId`（StrEnum）: `search_trends`・`search_historical_facts`・`fetch_sources`・`get_video_analytics`・
  `get_audience_retention`・`compare_video_performance`・`get_audience_demographics`・`get_channel_performance`。
- `ToolDef`（frozen）: `tool_id`・`version`・`description`・`input_model`・`output_model`（pydantic。JSON Schema は
  `model_json_schema()` から**導出**し手書きしない）・`effect`（初期は `read` のみ。`read` 以外の登録は契約テストで拒否）・
  `timeout_seconds`・`max_calls_per_run`・`cost_class`（`none` / `research_ledger` / `youtube_quota`）・
  `feature_flag`（tool 個別の有効化キー）。
- `ToolStatus`（StrEnum）— 結果は例外ではなく**状態**で返す（Agent が区別できる形）:
  `ok`・`no_data`（応答は正常で行が無い）・`insufficient_data`（標本・期間が足りない）・`withheld_by_threshold`（YouTube の
  privacy 閾値で隠れた可能性）・`reporting_delay`（直近日が未確定）・`not_available_via_api`・`permission_denied`（scope 不足）・
  `quota_exceeded`・`rate_limited`・`timeout`・`pending`（Research が未完。結果を待たずに戻る）・`provider_not_configured`・
  `not_supported`・`failed`。**取得できない値を架空の値で埋めない**（INV-58）。
- `ToolEnvelope`（全 tool の出力の包み）: `tool_call_id`・`tool_id`・`tool_version`・`status`・`data`（output_model | None）・
  `limitations`（文字列、機械可読コード付き）・`evidence_refs`（`kind:id`）・`retrieved_at`・`untrusted: Literal[True]`。
  **外部由来の文字列は全て `untrusted`**。LLM へは `<data>` 区切りで渡し、指示として扱わない（INV-57 の延長）。
- `tool_call_id = uuid5(TOOL_NAMESPACE, f"{run_idempotency_key}:{seq}:{tool_id}:{input_sha256}")`。Activity の retry で
  handler が同じ順序で同じ入力を作る（LLM 応答は `ReplayLlm` で再生）限り**同じ ID**になる。

### 2. 権限: Agent ごとの許可と版固定

- `AgentSpec` に `allowed_tool_ids: tuple[str, ...] = ()`（`"search_trends@1"` の形）を追加する。**`canonical()` は空の
  ときこのキーを出さない**ので、登録済み 1.0 spec の `manifest_hash` は変わらない（既存の固定テストが緑のまま）。
- 新 spec_version `1.1` を足す（`strategy@1.1`・`learning@1.1`・新 role `historian@1.0`）。1.0 は凍結のまま残す。
  - `strategy@1.1`: `search_trends`・`get_video_analytics`・`compare_video_performance`・`get_audience_demographics`・
    `get_channel_performance`。
  - `historian@1.0`: `search_historical_facts`・`fetch_sources`。
  - `learning@1.1`（Review の役）: `get_video_analytics`・`get_audience_retention`・`compare_video_performance`。
- 新 Template `shorts_agentic_tools@1`（`runnable`、`JPMAS2_AGENT_TOOLS` が有効なときだけ起動可）が 1.1 の spec を参照する。
  既存の `shorts_agentic@1` は**変更しない**（pin 済みの計画・履歴・replay に影響しない）。
- `historian` は `contracts.intervention.AgentRole` に足す。`ck_agent_runs_role` の張り替えは migration 0020。

### 3. Tool の実行は Activity の中だけ（Temporal 決定論）

- workflow は tool を知らない。`workers/pipeline/agentic_workflow.py` と `EpisodePipelineWorkflow` は**変更しない**。
  Tool は `run_agent_stage`（task queue `agents`）の中で `ToolGateway.call_tool` が実行する。
- **Tool は 1 stage につき plan → execute → synthesize の 1 往復だけ**。handler が LLM に「必要な tool 要求」を JSON で
  提案させ（`llm_json` 1 回目）、gateway が `allowed_tool_ids`・入力 schema・`max_calls_per_run` で検査して**逐次**実行し、
  結果を `<data>` に入れて 2 回目の `llm_json` で合成する。ループ・再帰・tool 結果からの追加要求は構造上できない
  （無限 tool 呼び出しの防止。`max_llm_calls` は 3 以下）。LLM の自由な tool 名は未知なら `ToolNotAllowedError`。
- **再試行で外部要求が重複しない**: `agent_tool_calls`（§5）に `tool_call_id` を UNIQUE で持つ。実行前に行を `started` で
  commit し、完了で `result` を保存する。retry は `tool_call_id` で保存済みの結果をそのまま返し、**外部を呼ばない**。
  - Research: `ResearchGateway.submit` の `idempotency_key = f"tool:{tool_call_id}"`。台帳（reserve→dispatch→spent）が
    二重課金を防ぐ。待ちは bounded（`timeout_seconds` まで DB をポーリングし heartbeat）。未完なら `pending` を返し
    **依頼は走り続ける**。同じ ID で再び呼べば同じ依頼に戻る。
  - Analytics: 読み取りだが quota を使う。`started` のまま結果が無い行（呼んだか不明）は再送せず、その run では
    `failed`（`ambiguous_prior_attempt`）にする。保存済みの結果があれば再利用する。
- **費用・上限**: Research は既存の `research_calls` 台帳と `ResearchLimits`（件数・金額・YouTube units）に従う。
  Analytics は `max_calls_per_run` と、日次の tool 呼び出し数の上限 `AGENT_TOOLS_DAILY_ANALYTICS_CALLS`（設定。既定は低い値）。
  **Analytics API の quota 単価は公式に記載が見つからない**ため、単価を仮定せず呼び出し回数で制限し、quota 使用量は
  `unknown` と記録する（0 にしない）。rate limit は同一 run 内の逐次実行と上限回数で足りるので**実装しない**（需要が出たら別 ADR）。

### 4. Research tool（既存 Searcher の adapter。再実装しない）

`infrastructure/agents/tools/research.py`（`workers/agents` から使う。`workers/planning` の境界テストには触れない）:

| tool | 内部の呼び出し | 備考 |
|---|---|---|
| `search_trends` | `TrendResearchSubmit`（`region`・`seed_terms`・`audience_hypothesis`）→ submit/起動/ポーリング → `TrendArtifact` を要約 | 「20代」は仮説文で**実測ではない**。`region` は視聴可能地域フィルタ |
| `search_historical_facts` | `EvidenceResearchSubmit`（`claim_inputs` ≤12）→ `EvidenceArtifact` | 各 claim の assessment（supported/qualified/disputed/insufficient）を**そのまま**返す。数値の信頼度を作らない |
| `fetch_sources` | `EvidenceArtifact.sources` の参照ビュー（url・title・published_at・retrieved_at・content_sha256・fetch_status） | **任意 URL の取得は実装しない**（`not_supported`）。`HttpContentFetcher` を台帳・予算の外で呼ぶ経路を作らない |

- `RESEARCH_PROVIDER` が `none` なら `provider_not_configured`。`fake` の結果には `limitations` に `provider=fake` を入れ、
  実調査と区別できる形で返す。**実 Provider の配線はこの ADR の範囲外**（別 ADR）。
- 戻り値に外部ページの抜粋（`excerpt`）は入れない。ID・assessment・メタデータ・`evidence_refs` のみ。
- 保存: Query・Source URL・Title・Retrieved/Published At・Evidence ID・Episode ID・検証状態は Research の artifact にある。
  **Agent ID と Tool Call ID は `agent_tool_calls` が持ち**、`research_request_id` で Research の依頼と結びつける
  （Research 側の表・契約は変更しない。`requester = "agent:<role>@<spec_version>"` を使う）。

### 5. Analytics tool（既存 adapter の拡張。OSS は採らない）

- 新 port `domain/analytics/ports.py::VideoInsightProvider`（read-only メソッド 5 つ。video_id・期間を引数に取る）と、
  `infrastructure/analytics/youtube_analytics.py` の拡張実装（`reports.query`、`channel==MINE`、GET のみ）。
  既存の `AnalyticsProvider.fetch` と `analytics_snapshots` は**変更しない**（Planner の経路を壊さない）。
- scope は `yt-analytics.readonly`（+ 動画の公開日・尺に `youtube.readonly`）のみ。**書き込み scope・収益 scope は要求しない**。
- 失敗の分類（`ToolStatus`）: 403 の reason を読んで `quota_exceeded` と `permission_denied`（tokeninfo で scope 確認）を分ける。
  応答 200 で `rows` が空なら `no_data`、`endDate` が要求より前に切り詰められたら `reporting_delay`、年齢・国など閾値対象の
  次元で行が欠けたら `withheld_by_threshold`（0 と区別する）。取得できない指標（Studio 専用の impressions/CTR など）は
  `not_available_via_api`。
- `compare_video_performance`: 公開後経過日数を揃える（`day` 次元、公開日起点）。Shorts と長尺は `creatorContentType` で
  分け、**混ぜて順位付けしない**。2025-03-31 に Shorts の `views` の定義が変わったため、比較の軸は `engagedViews`・
  `estimatedMinutesWatched`・`averageViewDuration`。2025-04-30 以前をまたぐ比較は `insufficient_data`。動画尺の差は
  `averageViewPercentage` で正規化し、標本が閾値（設定。既定 5 本）未満なら `insufficient_data`。
- `get_audience_demographics`: `viewerPercentage` は次元間で正規化されない（公式）ので合算しない。

### 6. 永続化: 新表は 1 つだけ（migration 0020）

- `agent_tool_calls`: `tool_call_id`（PK）・`agent_run_key`（= `agent_runs.idempotency_key`）・`episode_id`・`agent_role`・
  `agent_spec_version`・`seq`・`tool_id`・`tool_version`・`input_sha256`・`input`（JSON）・`status`・`result_object_key` +
  `result_sha256`（本体は MinIO）・`evidence_refs`（JSON）・`research_request_id?`・`started_at`・`finished_at`・`duration_ms`・
  `error_code`。UNIQUE `(agent_run_key, seq)`。
- `ck_agent_runs_role` と `ck_artifact_metadata_type` の張り替え（`historian` / `fact_check_report` の語彙追加）。
- **改善サイクルの追跡**に新しい関係表は作らない:
  Analytics Snapshot / Research Evidence = `agent_tool_calls`（+ MinIO の結果）、Strategy Proposal = `strategy_brief` artifact、
  Agent Decision = `decision_records`（payload の `citations` に `tool_call:<id>` を入れる）、Episode / Published Video =
  既存、Review Result = `quality_review`、Improvement Hypothesis = `lesson`（status=`proposed`）。つながりは
  `revision_links(derived_from)` と `agent_tool_calls.agent_run_key → agent_runs`。`StrategyBrief` / `Lesson` の
  `citations` に `tool_call:<uuid>` を入れる（既存の `citations` フィールドで足りるか実装で確認し、足りなければ
  この ADR を更新して schema_version を上げる）。

### 7. 人間の介入（既存経路の再利用）

- 閲覧: 読み取り API `GET /v1/intervention/episodes/{episode_id}/tool-calls`（`apps/api/routers/application.py` の隣、
  認証・flag は application API と同じ）。結果本体は MinIO の参照＋要約。
- 判断の承認・差し戻し・誤った根拠の指摘は**既存の command**（`approve`/`reject`/`comment` 相当、`annotation`）を使い、
  `target = tool_call:<id>` を参照できるようにするだけ（新 command type は作らない）。command_id の冪等性・hold・scope は
  そのまま。UI は後続（API と永続化を優先）。

### 8. Observability

- 構造化ログ（OpenSearch）: `EventName` に `agent.tool.completed`・`agent.tool.refused` を足す（`contracts/log_contract.py` と
  `docs/operations/log-contract.md` を同じコミットで更新）。属性: `tool_call_id`・`tool_id`・`tool_version`・`agent_role`・
  `episode_id`・`status`・`duration_ms`・`cost_class`・`cache_hit`。**引数・結果の本文・token・個人情報は出さない**。
- Metrics: 新しい依存を入れず、**ログイベントから OpenSearch の集計で出す**（呼び出し回数・成功/失敗率・所要時間・
  token（既存の LLM 記録）・quota 呼び出し数）。Prometheus / OTel は未導入なので導入しない。
- Traces: `episode_id` → `agent_run_key` → `tool_call_id` の親子と `duration_ms` をログに持たせ、OpenSearch で辿れるようにする。
  OTel の exporter は入れない。`ToolObserver` port（`OperationObserver` と同形）だけ置き、将来 OTel を差せる口にする。
  → 要件の「Metrics / Traces」は**ログ由来**で満たし、専用基盤は未導入であることを Consequences に負債として残す。

### 9. Feature Flag

`infrastructure/config.py::Settings`（既定すべて無効）:

| env | 既定 | 効果 |
|---|---|---|
| `JPMAS2_AGENT_TOOLS` | `false` | Tool Registry の組み立て・`shorts_agentic_tools@1` の起動・`tool-calls` API。OFF なら `ToolGateway` は従来どおり |
| `JPMAS2_ENABLED_TOOLS` | 空 | 有効な tool_id（カンマ区切り）。空なら何も有効でない。未知の ID は起動時に失敗 |

OFF のとき: 既存 spec 1.0・Template・workflow・日次経路は変更なし。`agent_tool_calls` は書かれない（INV-55 の延長）。

### 10. 不変条件（番号は 58〜、テストと同じコミットで追加）

| ID | 内容（要旨） |
|---|---|
| INV-58 | Tool は許可された `tool_id@version` だけを、宣言した上限内で呼ぶ。結果は状態で返し、取得できない値を架空の値で埋めない（欠測・遅延・権限不足・データ不足・API 非対応を区別） |
| INV-59 | Tool の外部要求は Activity の retry で重複しない（`tool_call_id` の保存済み結果を再利用。Research は台帳、Analytics は ambiguous を再送しない） |
| INV-60 | 外部由来の文字列は `untrusted`: tool 名・指示・URL の生成に使わない。tool は 1 stage 1 往復で再帰できない |
| INV-61 | Tool は read-only: 投稿・削除・更新・書き込み scope を要求する経路を持たない |
| INV-62 | Tool の定義は `contracts/agent_tools.py` の 1 か所。schema は model から導出し、API / 実装 / 文書が一致する |

## Alternatives

- **Pydantic AI + FastMCP を採用**: 現行 Agent は「事前取得した documents → LLM 1 回」で tool ループが無く、Pydantic AI の
  中核価値が使われない。`AgentSpec`・`ToolGateway`・予算 gate・予約台帳・`ReplayLlm`・`manifest_hash` と責務が重なり、
  作り替えると INV-51/56 を同時に作り直す。公式の Temporal 連携は model/tool 呼び出しを個別の Activity にする設計で、
  「1 stage = 1 Activity」と噛み合わず（`TemporalAgent` は deprecated 予定、tracer は experimental）、Codex CLI 用の Model も自作が要る。
  推移的に 30 超のパッケージ（`httpx2`・`mcp>=2`・`authlib` 等）を `constraints.txt` に追加し、週次で動く API を追うことになる。
  FastMCP は MCP を**公開**する側の道具で、内部 tool 層には使えない。**再評価の条件**: tool が副作用を持つ多段ループになる、
  または Codex 以外の API 型 LLM を採用する。
- **LangGraph / LangChain / OpenAI Agents SDK**: 状態機械・永続層が Temporal と二重になる。公式の Temporal 連携が無い、または
  特定版への完全固定を要求する。採らない。
- **Analytics MCP を採用（4 件）**: 全て別プロセス・作成が新しく・token を別の平文ファイルに置く。pauling-ai は書き込み tool
  が多数で AGENTS.md §9 と相性が悪い。ADR-0025 が MCP 依存を退けた理由（保守不能）と同じ。既存 adapter が OAuth・秘匿・401/403
  診断・fallback・テストを持つ。**アイデアだけ借りる**（retention の組み方・経過日数を揃えた比較・非 GET の拒否）。
- **今すぐ MCP Server を公開**: 利用者がいない transport を作ることになり、認可（principal と scope の対応）の設計が先に要る。
  Registry の上の薄い adapter として後から足せる形（schema を `ToolDef` から導出）だけを保つ。
- **Tool の結果を Temporal の履歴に載せる**（Activity 分割）: 履歴が膨らみ、外部由来の文字列が履歴に残り、既存 workflow の
  replay 互換を壊す。MinIO + `agent_tool_calls` に置く。
- **Tool 結果に数値の信頼度を付ける**: 根拠が無い。Research の assessment（4 段階）と取得状態をそのまま返す。
- **OTel / Prometheus を今入れる**: 新規依存と基盤（collector）が要る。ログ由来で足りる範囲を先に確認する。

## Consequences

良い点:

- 新規 Python 依存ゼロ。既存 spec 1.0 の `manifest_hash`・日次経路・workflow の replay は変わらない。
- tool 呼び出しが `tool_call_id` で入力・出力・判断・根拠に結びつき、後から機械処理で検証できる。

引き受ける負債:

- **実 Research Provider が未配線**のため、`search_trends` / `search_historical_facts` は本番設定（`none`）では
  `provider_not_configured`、`fake` では固定コーパスの結果になる。実調査には別 ADR（Provider 配線）が要る。
- `fetch_sources` は任意 URL を取得できない（evidence 内の source の参照のみ）。
- Analytics API の quota 単価は未確認。呼び出し回数で制限し、quota 使用量は `unknown`。
- 本番で `yt-analytics.readonly` が付与済みかは未確認。未付与なら全 Analytics tool は `permission_denied` を返す。
- Metrics / Traces は専用基盤ではなく**ログ由来**。OTel を入れる場合は別 ADR と `constraints.txt` の追加が要る。
- tool 往復を 1 回に固定したため、「結果を見て追加検索」はできない（意図した制約。緩めるなら ADR）。
- `strategy@1.1` / `learning@1.1` の追加で spec が増える。1.0 を使う Template は残り、当面は並存する。
- Historian は最小の handler（claim 抽出 → 根拠収集 → `fact_check_report`）で、台本への自動反映はしない（提案のみ）。
- 運用 UI は後続（API と永続化のみ）。
