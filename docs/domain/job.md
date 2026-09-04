# Job

## 定義

**Job = 1つのActivityの1回の実行記録**。「何を試みて、どうなったか」の監査ログ。

Jobは**スケジューリングの単位ではない**。前身repoの `jobs` テーブルは
lease付きのワークキューだったが、v2ではその役割をTemporalが完全に担う。
v2のJobは**書き込み先が主に事後**であり、誰もJobをpollしない。

## 同一性

`job_id`。同じ工程を3回試みたら3つのJobが残る。
Phase 1 の生成は `uuid.uuid4()`（[[episode]] と同じ。UUIDv7 は Phase 2 の課題）。

## 属性

- `id`, `episode_id`, `type`（`dummy` / 将来 `plan` / `generate` / `render` / `upload` ...）
- `attempts` — これまでに実施した試行回数。Activity開始のたびに加算する
- `max_attempts` — 試行枠。TemporalのRetryPolicyの `maximum_attempts` と同じ値を使う
- `status`: `queued` / `running` / `succeeded` / `retryable_failed` /
  `terminal_failed` / `skipped`（ADR-0006）
- `failure_class` — 失敗時のみ。`transient` / `retryable` / `needs_input` / `permanent`
- `error_summary` — 人間向け。**この文字列を機械判定に使わない**
- `input_hash` — 入力Artifact群のハッシュ。再開判定に使う
- `output_artifact_ids`
- `started_at`, `finished_at`
- `cost_jpy` — その試行で発生した課金
- `workflow_id`, `activity_id` — Temporal参照（相関用、権威ではない）

## status と failure_class の関係

status は「今どうなっているか」、failure_class は「なぜそうなったか」。
写像は `domain/job/transitions.py` の `job_event_for_failure()` に**1つだけ**置く。

| failure_class | job status |
|---|---|
| `transient` / `retryable` | `retryable_failed`（試行枠が残る間） |
| 同上・枠切れ | `terminal_failed` |
| `needs_input` / `permanent` | `terminal_failed`（retryしない） |

`queued` からも失敗へ遷移しうる。Activityが `running` を書く前に落ちる経路
（開始直後のDB障害など）が実在するため。

**Job が `terminal_failed` でも Episode は terminal とは限らない。**
Episodeの遷移先は `episode_event_for_failure()` が失敗クラスからのみ決める（INV-12）。

## `skipped`

入力hashが一致する出力Artifactが既に存在したため、実処理をせずに
既存Artifactを返した場合。**再開が効いた証拠**であり、これが記録されないなら
冪等性が壊れている。

## 不変条件

- 失敗したJob（`retryable_failed` / `terminal_failed`）には必ず `failure_class` がある
- `failure_class` は例外型から導出する。散文のgrepで決めない
- `succeeded` のJobには必ず1つ以上の出力Artifactがある（出力の無い工程を作らない）。
  Phase 1 では `artifact_metadata.produced_by_job_id` が逆参照を持つ
- Jobの失敗は、そのEpisode以外に影響しない（INV-13）
- Jobは削除しない（監査ログ）

## Jobが持たないもの

- 次に何をするかの決定（INV-4）
- lease / 排他制御（Temporalが持つ）
- 成果物本体

## 機械検査

- 状態遷移: `tests/unit/test_job_transitions.py`
- 永続化とattempts加算: `tests/unit/test_repositories.py::test_job_lifecycle_is_persisted`
- retry時の遷移: `tests/integration/test_episode_workflow.py::test_activity_fails_once_then_retry_succeeds`
- 実装表: `domain/job/transitions.py` の `JOB_TRANSITIONS`
