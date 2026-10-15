# 自律改善型制作の Feature Flag と Agent 実行基盤（ADR-0043 §2・§5）

新機能はすべて既定で無効。日次経路（`DailyEpisodeWorkflow` / `EpisodePipelineWorkflow` / Topic Planner）は
これらの flag も Registry も参照しない（INV-55）。

## 1. Flag（`infrastructure/config.py::Settings`）

| env | 既定 | 効果 |
|---|---|---|
| `JPMAS2_APPLICATION_API` | `false` | storyboard 提案 route。無効なら `404 feature_disabled`（P0-B） |
| `JPMAS2_AGENTIC_WORKFLOWS` | `false` | agentic workflow / agent activity の登録と `StartAgenticProduction`（読み手は P1） |
| `JPMAS2_ENABLED_AGENTS` | 空 | 有効な agent role（`AgentRole` の値のカンマ区切り。例 `strategy,creative_director`）。空は全て無効 |
| `JPMAS2_AGENT_LLM` | `codex` | agent の LLM。`codex` / `fake` のみ。`fake` は隔離試験スタック専用の決定的な偽物 |

- `JPMAS2_ENABLED_AGENTS` の未知の名前は**起動時に失敗**する（誤記で黙って何も有効にならない状態を作らない）。
  読み取りは `enabled_agent_roles(settings)`。無効な role の stage は P1 で `agent_disabled` として止まる。
- `JPMAS2_AGENT_LLM=fake` を本番（compose の `avp2`）に設定しない。fake は外部へ出ないが、LLM を使う成果物が
  すべて定型になる。
- flag は env だけで変わる。HTTP・AI・Agent から変える経路は無い。compose への受け渡し（`agentic` profile）は P1。

## 2. 費用と予算（INV-56）

- 1 回の LLM 呼び出しの実績費用 = token usage × ModelPolicy の単価。usage か単価が欠ければ **`None`**
  （`agent_runs.estimated_cost_usd = NULL`、`token_usage` も NULL）。0 で埋めない。Codex の policy
  （`codex_cli_text@1`）は単価が無く、実績費用は常に不明。
- 予算は**予約の上限**で数える: Agent 段の開始前に `provider_reservations` へ `agent_llm` を
  `estimated_cost_usd = spec.max_estimated_cost_usd` で予約して commit し、その後で `mark_dispatched`
  （dispatch gate。hold 中は `EpisodeHeldError`、予約は未 dispatch のまま残る）。
- その key の**最初の**予約の前にだけ `budget_view` + `check_additional_cost` を検査する。
  `BUDGET_USD_JPY_RATE` が無く上限 > 0 なら `budget_unverifiable`、残りを超えるなら `budget_exceeded`。
  どちらも予約を作らない。再送（予約が既にある）は再検査せず同じ予約を返す。
- 別 key の evidence の無い `agent_llm` 予約が残っていれば新しい呼び出しを始めない（ADR-0013）。
- 実装: `infrastructure/agents/budget_gate.py`（`reserve_and_dispatch_agent_call` / `settle_agent_call`）。

## 3. 参照の棚卸し（AGENTS.md §8、2026-10-15）

`grep -rniE "<key>" apps/ workers/ domain/ infrastructure/ contracts/ docs/ application/`（ADR・設計書を除く）:

| key | 読み手 | 書き手（定義） | 無関係 |
|---|---|---|---|
| `jpmas2_agentic_workflows` | 0 件（P1 が読む） | `infrastructure/config.py` 1 件 | 0 |
| `jpmas2_enabled_agents` | `enabled_agent_roles` 1 件 | `infrastructure/config.py`（定義・検証）2 件 | 0 |
| `jpmas2_agent_llm` | 0 件（P1 の worker が読む） | `infrastructure/config.py` 1 件 | `fake_llm.py` の説明 1 件 |

## 4. 所有者の CLI `scripts/agentic.py`（P1-C）

DB（`DATABASE_URL`）・MinIO・Temporal に**直接**つなぐ所有者用の道具。HTTP は P2。環境変数は他の運用 script と同じ
（`infrastructure/config.py`）。公開設定・予算上限・Schedule を渡す引数は無い（投稿は private のみ、AGENTS.md §9）。

| コマンド | 内容 |
|---|---|
| `plan --template shorts_agentic [--template-version 1]` | コンパイル結果の確認。DB・Temporal に触れない。拒否は `{"error": {"code": ...}}` で exit 1 |
| `plan --episode EP` | Episode に pin 済みの計画（無ければ `no_pinned_plan`） |
| `draft --theme T [--template ...] [--tone ...] [--audience ...] [--duration N] [--must-keep X ...] [--notes ...] [--command-id ID]` | Episode・UserBrief・`episode_controls(auto_intervenable)` を作る。**生成は始めない**。`--command-id` を省くと uuid4 を stderr に出す。同じ id・同じ内容の再送は同じ Episode |
| `start EP` | `AgenticEpisodeWorkflow` を起動（`JPMAS2_AGENTIC_WORKFLOWS=true` 必須。無効なら `feature_disabled`） |
| `learn EP` | `AgenticLearningWorkflow` を起動（同上。測定が揃うまで何度でも起動できる） |

終了コード: 0 = ok、1 = 拒否（stdout の JSON に `code`）、2 = 使い方・入力。

### 有効にする手順（隔離スタックで）

1. 本番（compose project `avp2`）の worker には**設定しない**。`JPMAS2_AGENTIC_WORKFLOWS` と `JPMAS2_ENABLED_AGENTS` を
   env に足した pipeline worker だけが `AgenticEpisodeWorkflow` / `AgenticLearningWorkflow` と pin・採用 Activity を登録する
   （`workers/pipeline/run_worker.py::registrations`。既定の登録は変わらない）。agent worker（queue `agents`）は別に起動する。
2. 隔離試験は `deploy/logging/test/run-e2e.sh`（`AVP_APPTEST_PROJECT=avp2-oslog-c<suffix>`）だけを使う。LLM は
   `JPMAS2_AGENT_LLM=fake`（外部へ出ない）。有料 API・YouTube を呼ばない。
3. `scripts/agentic.py draft ...` -> `plan --episode EP`（pin 前は `no_pinned_plan`）-> `start EP` -> workflow の結果で
   `completed_stages` / `stopped_stage` / `reason` を見る。hold・待機は `parked`、agent の blocked・失敗は `stopped`
   （理由つき）。
4. 公開後の測定が揃ったら `learn EP`。private のままなら `awaiting_public_metrics`（Lesson を作らない）。

### Workflow の結果

`AgenticEpisodeResult{outcome: completed|parked|stopped, plan_hash, completed_stages, stopped_stage, reason, qa_repair_cycles}`。
`qa_repair_cycles` は QA が出した提案 command の数（自動適用はしない）。止まる理由の例: `plan not pinned: ...`
（`template_not_runnable` 等）、`agent stage blocked: budget_exceeded`、`agent stage failed: ApplicationError(agent_spec_drift)`、
`no candidate adopted: no_strategy_brief`、`upload skipped: ...`。

### 参照の棚卸し（AGENTS.md §8、2026-10-15）

`grep -rniE "jpmas2_agentic_workflows" apps/ workers/ domain/ infrastructure/ contracts/ application/ scripts/`:
読み手は `workers/pipeline/run_worker.py`（登録）と `scripts/agentic.py`（`Deps.agentic_enabled`、`start` / `learn`）の
2 件、書き手（定義）は `infrastructure/config.py` の 1 件。

