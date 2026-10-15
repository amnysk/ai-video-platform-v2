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
- flag は env だけで変わる。HTTP・AI・Agent から変える経路は無い。

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

## 4. Agent worker（P1-B、ADR-0043 §6）

- 入口: `workers/agents/run_worker.py`（task queue `agents`、Activity `run_agent_stage` だけ。workflow は持たない）。
  `JPMAS2_AGENTIC_WORKFLOWS` が true でなければ**何も登録せず、理由を出して終了**する（INV-55）。
- compose: サービス `agent-worker` は **`agentic` profile だけ**（`core` に入れない。既定の `make up` / `deploy-workers`
  では起動も更新もされない）。起動は所有者の明示的な操作:
  `docker compose --profile agentic up -d agent-worker`（本番スタック `avp2` に対しては、所有者の承認を得てから）。
  `tests/contract/test_compose_workers.py` / `test_deploy_workers.py` には「`agentic` 専用 service は deploy 対象外」
  「Codex を持つ service に `agent-worker` を含める」という明示的な例外を足してある。
- env（`compose.yaml` の `agent-worker`）: `JPMAS2_AGENTIC_WORKFLOWS`（既定 `false`）・`JPMAS2_ENABLED_AGENTS`（既定 空）・
  `JPMAS2_AGENT_LLM`（既定 `codex`）・`EPISODE_BUDGET_JPY`・`MONTHLY_BUDGET_JPY`・`BUDGET_USD_JPY_RATE`（既定なし。
  未設定なら stage は `blocked / budget_unverifiable`）・`CODEX_*`。YouTube / fal の秘密は持たせない。
- LLM: `codex` は `CodexCliStoryGenerator` を `CodexAgentLlm` に注入（`workers/*/run_worker.py` だけが codex adapter を
  import してよい / INV-18）。`fake` は外部へ何も出ない決定的な LLM だが、`run_worker` 単体では canned 応答を持たない
  （隔離 E2E が `llm_factory` を差し替える）。
- 提案の starter: `workers/agents/starter.py::TemporalCommandStarter`（`InterventionCommandWorkflow` を start。
  agent の scope は `read` / `comment` / `propose` で、apply はしない）。
- 隔離試験スタック（`deploy/logging/test/compose.apptest.yaml`）には**まだ入れていない**。P1-D が agent worker を
  必要とするときは、同じ service 定義に `JPMAS2_AGENT_LLM=fake`・`JPMAS2_AGENTIC_WORKFLOWS=true`・
  `JPMAS2_ENABLED_AGENTS=...` を渡し、`PublicMetricsPort` の mock と canned の `llm_factory` を持つ入口を使う。

### 参照の棚卸し（AGENTS.md §8、2026-10-15、P1-B 実装後）

`grep -rniE "<key>" apps/ workers/ domain/ infrastructure/ contracts/ docs/ application/`（ADR・設計書を除く）:

| key | 読み手 | 書き手（定義） | 無関係 |
|---|---|---|---|
| `jpmas2_agentic_workflows` | `workers/agents/run_worker.py` 2 件 | `infrastructure/config.py` 1 件 | 0 |
| `enabled_agent_roles` / `jpmas2_enabled_agents` | `workers/agents/run_worker.py` 2 件 | `infrastructure/config.py` 2 件 | 0 |
| `jpmas2_agent_llm` | `workers/agents/run_worker.py` 2 件 | `infrastructure/config.py` 1 件 | 0 |
