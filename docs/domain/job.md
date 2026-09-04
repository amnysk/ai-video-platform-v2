# Job

## 定義

**Job = 1つのActivityの1回の実行記録**。「何を試みて、どうなったか」の監査ログ。

Jobは**スケジューリングの単位ではない**。前身repoの `jobs` テーブルは
lease付きのワークキューだったが、v2ではその役割をTemporalが完全に担う。
v2のJobは**書き込み先が主に事後**であり、誰もJobをpollしない。

## 同一性

`job_id`（UUIDv7）。同じ工程を3回試みたら3つのJobが残る。

## 属性

- `id`, `episode_id`, `stage`（`plan` / `generate` / `render` / `upload` / ...）
- `attempt` — そのstageの何回目か
- `status`: `running` / `succeeded` / `failed` / `skipped`
- `failure_class` — 失敗時のみ。`transient` / `retryable` / `needs_input` / `permanent`
- `error_summary` — 人間向け。**この文字列を機械判定に使わない**
- `input_hash` — 入力Artifact群のハッシュ。再開判定に使う
- `output_artifact_ids`
- `started_at`, `finished_at`
- `cost_jpy` — その試行で発生した課金
- `workflow_id`, `activity_id` — Temporal参照（相関用、権威ではない）

## `skipped`

入力hashが一致する出力Artifactが既に存在したため、実処理をせずに
既存Artifactを返した場合。**再開が効いた証拠**であり、これが記録されないなら
冪等性が壊れている。

## 不変条件

- `failed` のJobには必ず `failure_class` がある
- `failure_class` は例外型から導出する。散文のgrepで決めない
- `succeeded` のJobには必ず1つ以上の `output_artifact_ids` がある
  （出力の無い工程を作らない）
- Jobの失敗は、そのEpisode以外に影響しない（INV-13）
- Jobは削除しない（監査ログ）

## Jobが持たないもの

- 次に何をするかの決定（INV-4）
- lease / 排他制御（Temporalが持つ）
- 成果物本体
