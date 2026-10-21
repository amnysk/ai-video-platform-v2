# 日次異常レポート（窓・重複除去つき、読み取り専用）

```bash
python scripts/daily-anomaly-report.py            # 人が読む表
python scripts/daily-anomaly-report.py --json     # 機械向け
python scripts/daily-anomaly-report.py --no-temporal
```

終了コード: 0 = いま再発中の未解決 incident なし / 1 = あり / 2 = 実行できなかった。
PostgreSQL は `READ ONLY` トランザクションで SELECT だけ、Temporal は UI の HTTP API を GET するだけ
（`--temporal-url`、既定 `http://localhost:8233`）。Temporal 本体のコンテナ内で CLI を走らせない。
OpenSearch・provider・MinIO には触れない（`tests/architecture/test_anomaly_report_read_only.py`）。

## 数え方（「累積250」を失敗数と読まない）

`operational_anomalies` は Episode ごとに1日1行で、watchdog が毎時 `occurrences` を加算する。
停滞している Episode は毎日新しい行が増えるので、**行数 = 検出の累積であって失敗数ではない**。

| 列 | 意味 |
|---|---|
| `rows` | 窓に入った検出行（重複を含む。従来の累積） |
| `incidents` | 窓で見えた distinct な incident（`incident_key` で畳む） |
| `new_incidents` | 初検出が窓の中にある incident（本当に新しい失敗） |
| `unresolved` | 最新の行が未解決の incident（全期間） |
| `recurring_unresolved` | 未解決で、最後の検出が直近24hにあるもの（いま再発中） |

`incident_key`: watchdog 系 = `<kind>:<episode_id>`、provider 失敗 = `<episode>:<job type>:<scene>:<正規化した原因>`、
workflow 失敗 = workflow id、missed = 日付。`NOT_COMPLETED` / `NOT_UPLOADED` の原因は同じ Episode の
`STAGE_STALLED` の理由に寄せる（これらは停滞の結果で、原因は detail に無いため）。

カテゴリ間の重複: 同じ根本原因が複数カテゴリに現れる（例: 日次 workflow が失敗した日は
`workflow_failure` と `missed_schedule_run` の両方に1件ずつ、停滞 Episode は `stage_stalled` と
`not_completed_by_deadline` の両方に出る）。**カテゴリの合計を足して失敗数にしない。**

## カテゴリと出典

| カテゴリ | 出典 | 解決の判定 |
|---|---|---|
| `stage_stalled` / `not_completed_by_deadline` / `not_uploaded_by_deadline` | `operational_anomalies`（Episode 系3種） | `resolved_at` |
| `provider_error` | `jobs`（終端失敗で、`error_summary` に fal を含む） | Episode が完成点以降（render_ready/approved/uploaded/analyzed/completed/cancelled） |
| `render_failure` / `upload_failure` | `jobs`（`render_final_video` / `upload_final_video` の終端失敗） | 同上 |
| `workflow_failure` | Schedule `avp-daily-episode` の直近の起動のうち `FAILED` | 常に未解決（履歴は1日で消える） |
| `missed_schedule_run` | `daily_episode_slots` の最初の日から今日（JST、6:30 以降）まで、slot も Episode も無い日 | 常に未解決 |

## 既知の限界

- Temporal の保持期間は 1 日（`workflowExecutionRetentionTtl=86400s`）。失敗した日次 workflow の原因は
  24h 以内なら履歴から読めるが、それより古い実行は `unknown (history expired)` になる。Schedule の
  `recentActions` は直近10件だけ。古い原因は worker の `docker logs` や OpenSearch で補う。
- OpenSearch が止まっている間（収集は Fluent Bit のバッファ待ち）は `deploy/logging/queries/` の検索は使えない。
  ログは検索用の副本（INV-38）なので、レポートの正は PostgreSQL。
- `provider_auth_incidents` は fal の 401/403 を拾う設計だが、2026-10-21 時点で0行。403 は `jobs.error_summary` /
  `provider_reservations.error_summary` にだけ残っている。レポートは後者から数える。
- 同じ原因の incident を `common_cause` で示すのは、その原因が1種類だけの時。正規化後の文字列が
  1文字でも違えば別原因に数えるので、`causes` の内訳を読むこと。

## release preflight から使う入口

```python
from infrastructure.diagnostics.anomaly_events import recurring_unresolved_by_category

counts = await recurring_unresolved_by_category(session, now, reader)  # {"stage_stalled": 15, ...}
```

`reader` は `TemporalReader`（`snapshot()` を持つ）。`None` なら Temporal 由来の2カテゴリ
（`workflow_failure` と、原因付きの `missed_schedule_run`）は PostgreSQL 側だけで数える。配線は preflight 側の担当。

## OpenSearch の検索・アラート定義（適用しない）

- `deploy/logging/queries/daily-anomaly-queries.json`: `_search` 本文（size 0 の集計だけ）
- `deploy/logging/alerts/daily-anomaly-monitors.json`: Alerting の query monitor 定義。**`enabled:false`・
  `actions:[]`・未適用**。適用は所有者の判断（`POST _plugins/_alerting/monitors`）。Alerting plugin への適用は未検証。
- 使うイベント名はすべて `docs/observability/emission-points.md` に載っているもの（契約テストで検査）。
