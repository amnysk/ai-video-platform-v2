# research-worker の運用

Status: **Accepted (2026-09-29)**（ADR-0037 §8.5）

Research（Trend / Evidence）の依頼を実行する常駐 worker。**補助機能**であり、Episode の日次・pipeline・
企画・台本・制作・投稿はこの worker を起動しない・待たない（INV-37）。research-worker が止まっていても、
Provider が未設定でも、Episode の工程は変わらずに進む。

## 1. 構成

| 項目 | 値 |
|---|---|
| compose service | `research-worker`（共通の worker イメージ、`core` profile） |
| module | `workers.research.run_worker` |
| task queue | `research`（`contracts/research.py::RESEARCH_TASK_QUEUE`）。Workflow と Activity の両方 |
| health | `poller_check --queue research`（既定の max-age） |
| 環境 | DB・MinIO・Temporal と `RESEARCH_PROVIDER` だけ。`YOUTUBE_*` / `CODEX_*` / `FAL_KEY` は渡さない |
| 書く場所 | `research_requests` / `research_calls` / `research_artifacts` の表と、MinIO の `research/` 接頭辞だけ（本番の表と `artifacts/` には書かない） |

依頼の受け付けは api（`POST /research/requests`）。api も同じ `RESEARCH_PROVIDER` を読んで受け付けの門に使う。

## 2. Provider（`RESEARCH_PROVIDER`）

| 値 | 動作 |
|---|---|
| `none`（**既定**） | 外部を一切呼ばない。依頼は受け付けの時点で `blocked`（理由コード `provider_not_configured`）になり、workflow も起動しない |
| `fake` | 固定コーパス（`infrastructure/research/fake_corpus.py`）の Fake 検索・Fake 取得。実ネットワークに出ない |

- **実 Provider（Web 検索・YouTube 検索・本文の HTTP 取得）は配線していない**。選ぶのは所有者の判断で、
  ADR を先に置く（ADR-0036 / ADR-0037 §6）。実 Provider では金額と quota の上限（`RESEARCH_MAX_COST_USD` /
  `RESEARCH_MAX_YOUTUBE_UNITS`）が両方とも要り、どちらかが無ければ呼ぶ前に `blocked`（`budget_not_set`）。
- `.env` の `RESEARCH_PROVIDER` は api と research-worker の**両方**が読む。片方だけ変えると、受け付けか実行の
  どちらかの門で `blocked` になる（安全側）。変えたら両方を作り直す:
  `docker compose --profile core up -d api research-worker`。
- **種別ごとの Handler はまだ無い**（Evidence は ADR-0038、Trend は ADR-0039 で入る）。`fake` にしても、今は
  workflow が走って `blocked`（`handler_not_available`）で終わる。外部も Fake も呼ばない。

## 3. 依頼の流れと状態

```
POST /research/requests ─ 門（Provider・予算）─┬─ blocked（起動しない）
                                            ├─ 鮮度内の完了済みを再利用（起動しない。reused=true）
                                            └─ queued → ResearchWorkflow（research-worker）
                                                   └ research_execute（上限つき retry）
                                                        → completed / partial / blocked / failed
```

- 状態の正本は DB。`GET /research/requests/{id}` は DB だけを読む（`status`・`blocked_reason`・`result`）。
- `partial` は合格ではない。呼び出し側は `completed` 以外を「調査なし」として扱う。
- 一時障害は `research_execute` を最大 3 回（1 回目を含む）試す。retry も呼び出しの枠を数える（INV-36）。
  使い切ったら `failed`（`execution_failed`）。
- worker が実行中に落ちた場合は heartbeat timeout（2 分）で retry される。dispatch 済みで結果の無い呼び出しは
  送り直さず、依頼は `blocked`（`ambiguous_call`）になる。人が照合するまで再開しない。

## 4. 手順

### 起動・確認

```bash
docker compose --profile core up -d research-worker
docker compose --profile core logs -f --tail=100 research-worker   # make workers-logs でも見える
scripts/smoke-workers.sh                                           # research-worker が queue research を poll しているか
```

### 依頼を出す（Fake で試す）

1. `.env` を `RESEARCH_PROVIDER=fake` にし、`docker compose --profile core up -d api research-worker`。
2. `POST /research/requests`（本体は `contracts/research.py` の `EvidenceResearchSubmit` / `TrendResearchSubmit`。
   `idempotency_key` 必須）。202 の `workflow_id` が `research-{request_id}` なら起動した。
3. `GET /research/requests/{request_id}` で状態を見る。今は Handler が無いので `blocked`
   （`handler_not_available`）で終わる（§2）。
4. 試し終えたら `RESEARCH_PROVIDER=none` に戻す。

同じ `idempotency_key` の再 POST は同じ依頼を返す（`queued` のままなら同じ workflow id で起動し直す。
保存と起動の間で api が落ちた依頼はこれで回収する）。同じキーで内容が違えば 409。

### `blocked` の依頼を再開する

原因（Provider の設定など）を直してから `POST /research/requests/{id}/resume`。門を通らない（Provider が
`none` のまま、凍結した上限が足りない）・`blocked` でない依頼は 409 で、何も変えない。上限は再開で増えない
（新しい `idempotency_key` で出し直す）。`ambiguous_call` の依頼は、台帳（`research_calls`）の dispatch 済みの
行を照合してから判断する。

### 止める

`docker compose --profile core stop research-worker`。Episode の工程には影響しない。走行中の依頼は
worker が戻ったときに続きから実行される（成功済みの呼び出しは送り直さない）。

## 5. してはいけないこと

- research-worker に `YOUTUBE_*` / `CODEX_*` / `FAL_KEY` や実 Provider の鍵を渡さない
  （`tests/contract/test_research_worker_compose.py`）。
- テスト・CI から実 Provider を呼ばない（AGENTS.md §9）。
- Episode の workflow や日次の Schedule から Research を起動・待機させない（INV-37、
  `tests/architecture/test_daily_does_not_wait_for_research.py`）。
