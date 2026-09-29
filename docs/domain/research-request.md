# ResearchRequest

## 定義

**ResearchRequest は、調査（Trend / Evidence）1 件の依頼とその実行状態の正本である**（ADR-0037）。
Episode の Job / Artifact / 予約とは別の表に置く（INV-37）。Episode を持たない調査（Trend はチャンネル
単位）を、架空の Episode を作らずに保存するための単位である。

## 表（migration 0015、`infrastructure/db/models.py`）

| 表 | 1 行の意味 | 主な制約 |
|---|---|---|
| `research_requests` | 依頼 1 件 | `idempotency_key` UNIQUE、`kind` / `status` の CHECK。`episode_id` は参照だけで FK は張らない |
| `research_calls` | 外部呼び出し 1 件（呼ぶ前に予約する） | `UNIQUE(request_id, provider_call, call_seq)`、`call_seq >= 1`、`idempotency_key` UNIQUE、`reserved` ⇔ `settled_at IS NULL`、`abandoned` ⇒ `dispatched_at IS NULL` |
| `research_artifacts` | research 所有の成果物 1 世代 | `(request_id, artifact_type)` ごとに現行は 1 本（部分一意索引）。`(…, sha256)` と `(…, version)` も一意 |

FK は `research_calls.request_id` → `research_requests.id` と `research_artifacts.request_id` →
`research_requests.id` の 2 本だけで、どちらも `ON DELETE RESTRICT`。行を持つ依頼は削除できない。
downgrade は research の行が残っていれば拒否する。

## 同一性

- `request_id = uuid5(RESEARCH_NAMESPACE, idempotency_key)`（`domain/research/ids.py`）。
- `request_hash`（`domain/research/identity.py`）は意味の指紋で、再利用の判定に使う。リポジトリが保存する
  payload から計算する。同じキーで hash が違う依頼は `ResearchIdempotencyConflictError` になる。

## 状態機械（`domain/research/status.py` が唯一の定義）

Episode の状態機械（INV-8）とは別の表である。表に無い遷移は `InvalidTransitionError` になり、何も書き込まない。

| 現在 | 事象 | 次 |
|---|---|---|
| `queued` | `started` | `running` |
| `running` | `completed` | `completed`（終端） |
| `running` | `partial` | `partial`（終端） |
| `running` | `blocked` | `blocked` |
| `running` | `failed` | `failed`（終端） |
| `blocked` | `resumed`（人が原因を直して再開） | `queued` |

- `partial` は合格ではない。`find_reusable` は `partial` を返さない。
- `completed` / `partial` にできるのは、結果が指す成果物がこの依頼の現行の `research_artifacts` 行として
  記録済みのときだけ。
- `blocked` は `blocked_reason.code` が必須で、終端ではない。
- 実行状態と主張の評価は別物である。「正常に調べて根拠が無かった」は `completed`（または `partial`）になる。

## 外部呼び出し台帳（`domain/research/calls.py`、INV-36）

| 現在 | 事象 | 次 | 条件 |
|---|---|---|---|
| `reserved` | `spent` | `spent` | 呼んだ（成否にかかわらず） |
| `reserved` | `abandoned` | `abandoned` | dispatch 前だけ |

枠の単位は種別（`search` / `fetch` / `assess`）で、上限は `contracts.research.call_ceiling` で決まる。
予約の手順と拒否の理由は ADR-0037 §4 に書いてある。

## リポジトリ（`infrastructure/db/research_repositories.py`）

| クラス | メソッド |
|---|---|
| `ResearchRequestRepository` | `create_or_get` / `get` / `get_by_idempotency_key` / `mark_running` / `finish` / `resume` / `list_unstarted` / `find_reusable` |
| `ResearchCallRepository` | `reserve` / `mark_dispatched` / `mark_spent` / `mark_abandoned` / `list_for_request` / `find_ambiguous` |
| `ResearchArtifactRepository` | `record` / `find_current` / `list_current` |

## 読み手・書き手（AGENTS.md §8 の grep 記録）

`git grep -n "<key>" f209e7c -- apps workers domain infrastructure contracts docs` を base（f209e7c）で
実行した結果は、以下のキーすべてが 0 件だった（既存の読み手・書き手は無く、新しい語彙である）。

| キー | 件数 | 分類 |
|---|---|---|
| `research_requests` / `research_calls` / `research_artifacts` | 0 / 0 / 0 | 新規 |
| `call_seq` | 0 | 新規（旧ブランチの `provider_reservations.call_seq` は移植しない） |
| `ResearchStatus` / `ResearchKind` / `ResearchCall` / `ResearchArtifactType` | 0 | 新規（`contracts/research.py` が唯一の定義） |
| `RESEARCH_NAMESPACE` / `request_hash` / `max_assessments` | 0 | 新規 |

変更後の読み手と書き手は `contracts/research.py`（定義）、`domain/research/*`（読み手）、
`infrastructure/db/models.py` と migration 0015（DB の制約）、`infrastructure/db/research_repositories.py`
（書き手）である。本番のコードに読み手は無い（`tests/architecture/test_research_isolation.py`）。
