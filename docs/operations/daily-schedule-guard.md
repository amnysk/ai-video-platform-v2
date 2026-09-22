# Daily Schedule の維持（guard / watchdog）の運用

設計: [ADR-0027](../decisions/0027-daily-schedule-guard-and-watchdog.md)、契約: INV-25 / INV-26。
コマンドは host の venv から実行する（`DATABASE_URL` は host から見た接続先、`TEMPORAL_ADDRESS=localhost:7233`）。

## 1. 本番の望ましい状態

`avp-daily-episode` は **paused=false**、次回実行が 25 時間以内、`avp-daily-watchdog` も動いている。
確認:

```bash
python scripts/schedule-guard.py status          # 人が読む
python scripts/schedule-guard.py status --json   # 機械向け（Schedule・スイッチ・open な異常）
```

終了コード: 0 = 健全 / 1 = 異常あり（pause・不在・次回実行が不正・open な異常・DB に届かない）。

## 2. deploy を maintenance pause で包む

```bash
scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m -- make _deploy-workers-inner
```

`make deploy-workers` は上のように**内側の deploy を包む**（Makefile は別担当。配線は下の抜粋どおり）。

```make
deploy-workers:
	scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m -- $(MAKE) _deploy-workers-inner
```

`_deploy-workers-inner` は build → `up -d` → health check → `scripts/workers-versions.sh` までを行い、失敗したら非0で終わる。
ラッパーは内側の成否に関わらず終了時に `maintenance end`（unpause + describe 確認）を呼ぶ。

| 場面 | 挙動 |
|---|---|
| deploy 成功 | pause → deploy → unpause → describe で `paused=false`・次回実行が未来 |
| deploy 失敗（非0） | 同じく unpause してから、deploy の終了コードで終わる |
| Ctrl-C / SIGTERM | trap で unpause |
| `kill -9` / ホスト停止 | TTL 切れ（既定 30 分・最大 6 時間）で watchdog が解除する |
| 運用者が緊急停止中（begin が exit 3） | deploy は実行するが**解除しない** |
| begin 失敗（Schedule 不在・Temporal に届かない） | deploy を実行しない |
| end 失敗 | 非0で終わり、`schedule-guard.py reconcile` を促す |

`--ttl` は deploy の所要時間より長くする（短いと deploy 中に watchdog が解除しうる）。

## 3. 緊急停止（意図的な停止）と maintenance pause の区別

- 緊急停止は普通に pause する（`ensure-daily-schedule.py --pause` か `temporal schedule toggle --pause --reason "..."`）。
  印が無いので**ガードは決して解除しない**。watchdog は `SCHEDULE_PAUSED_UNEXPECTEDLY` として毎日記録し続ける
  （意図した停止でも記録される。解除は運用者が `ensure-daily-schedule.py --unpause`）
- 新しい Episode だけ止めたいなら DB スイッチ `paused`（`operational-switch.py`、workers.md §6）。
  Schedule は動き、Daily が Episode を作らないだけ。`status` に併記される

## 4. watchdog の導入（初回のみ・この順）

```bash
make migrate                                            # migration 0010（operational_anomalies）
docker compose up -d --build pipeline-worker            # DailyWatchdogWorkflow と検査 Activity を載せた版
python scripts/ensure-daily-schedule.py --watchdog      # dry-run: 登録内容を表示
python scripts/ensure-daily-schedule.py --watchdog --apply
python scripts/schedule-guard.py status
```

migration より前に watchdog を登録すると、検査 Activity が表の不在で失敗し続ける。
Schedule の再登録（`--apply`）は冪等で、現在の pause を保つ。

## 5. 異常の読み方

`operational_anomalies` — Schedule 系（下表の上5件）は1日1行、Episode 系（下表の残り4件、
ADR-0031）は **Episode ごとに1日1行**（`episode_id` 列で区別。部分インデックス2本で強制）。
ログは ERROR `OPERATIONAL_ANOMALY anomaly=<KIND> date=...`。

| kind | 意味 | 対処 |
|---|---|---|
| `DAILY_AUTOMATION_NOT_STARTED` | 予定 + 猶予を過ぎても slot も DailyEpisodeWorkflow も無い | `status` で Schedule と worker を確認。直ったら回復時に自動で閉じる |
| `SCHEDULE_PAUSED_UNEXPECTEDLY` | 印の無い pause が続いている | 意図した停止なら放置（記録は続く）。そうでなければ `--unpause` |
| `SCHEDULE_MAINTENANCE_OVERRUN` | maintenance pause が期限を過ぎ、ガードが解除した | deploy が `end` を呼べなかった。deploy 手順を確認 |
| `SCHEDULE_MISSING` | Schedule が存在しない | `ensure-daily-schedule.py --apply` |
| `SCHEDULE_NEXT_RUN_INVALID` | 動いているが次回実行が無い・遠い | describe を確認（cron・タイムゾーン） |
| `EPISODE_STAGE_STALLED`（ADR-0031） | Episode が `blocked` / `needs_work` のまま停滞猶予（既定 `stage_stall_grace_minutes`）を超えた | `detail.reason` / `detail.resumable` を見て、再開可能なら該当工程の POST で再開 |
| `EPISODE_NOT_COMPLETED_BY_DEADLINE`（ADR-0031） | 作成から完成期限（既定 `completion_deadline_hours`）を超えても `render_ready` 以降に達していない | 停滞中の工程を確認（多くは `EPISODE_STAGE_STALLED` と併発する） |
| `EPISODE_NOT_UPLOADED_BY_DEADLINE`（ADR-0031） | `render_ready`/`approved` 到達から投稿期限（既定 `upload_deadline_hours`）を超えても `uploaded` に届かない | `UPLOADS_PAUSED` なら意図した停止（このkindは記録されない）。そうでなければ upload worker を確認 |
| `PIPELINE_OUTCOME_MISMATCH`（ADR-0031） | `EpisodePipelineWorkflow` が Temporal 上は `completed` なのに型付き結果が `outcome=stopped` で、他のどの検査にも映らない | `detail.stopped_stage` / `detail.reason` を見て該当工程を確認。他の episode 系 kind と重複しないよう二重報告は避ける設計 |

```sql
select kind, anomaly_date, episode_id, occurrences, first_detected_at, resolved_at, notified_at
from operational_anomalies order by anomaly_date desc, kind;
```

`stage_stall_grace_minutes` / `completion_deadline_hours` / `upload_deadline_hours` の既定値は
`contracts/schedule_guard.py` の単一宣言元（`infrastructure/config.py` の Settings で上書き可能）。
Shorts 等の尺に固有の値をここ以外に埋め込まない。

通知は `AnomalyNotifier`（`infrastructure/observability/anomaly_notifier.py`）。既定はログのみ。
Slack / メール等は同じ Protocol を実装し、`workers/pipeline/activities.py` の
`PipelineActivities.notifier_factory` を差し替える（この1箇所が唯一の宣言元）。
`schedule-guard.py status --json` の `"notifier"` フィールドが `"log_only"` のままなら、
ログ以外の通知経路が無いことを意味する（`"configured"` になれば差し替え済み）。

## 6. 制限

- Temporal 自体が止まると watchdog も動かない。ホストの cron 等から `schedule-guard.py status`（exit 1 で異常）を
  呼ぶと別系統の監視になる
- watchdog が判定できる daily の cron は `M H * * *` の形だけ。それ以外は `unsupported_cron` で判定しない
- 検査は毎時 :35（JST）。始まらなかった日の検知は最長で予定 + 猶予 + 1 時間
- `EPISODE_STAGE_STALLED` 等の `resumable` は暫定判定（ADR-0031 §4）。ADR-0032（統一再開エントリポイント）の
  dry-run 判定に置き換わるまでの間だけ、既存の admit 表に基づく簡易判定を使う

## `make deploy-workers` との配線

`make deploy-workers [PYTHON=.venv/bin/python]` は `scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m`
で `scripts/deploy-workers.sh` を包む。成功・失敗・Ctrl-C のどれでも EXIT trap が `maintenance end`
（unpause → `paused=false` と次回実行が未来であることを describe で確認）を実行する。
Schedule が**運用者の pause（印なし）**のときは deploy だけ実行し、解除はしない（緊急停止を deploy が外さない）。

## 2026-09-20 の事故と、本番での確認記録

- 事故: 2026-09-19 04:21 頃、topic-planner の deploy のため `avp-daily-episode` を pause し、解除しないまま
  9/20 06:00 を迎えて Episode が作られなかった（pause は印なし = 運用者の pause 扱い）。
- 対策後の本番確認（2026-09-20）:
  - `make deploy-workers` を運用者 pause 中に実行 → 「運用者の pause。解除しない」で deploy だけ実行され、pause は維持された（緊急停止を外さない）
  - 検証完了後に `ensure-daily-schedule.py --unpause` → `Paused=false`、次回 2026-09-21 06:00 JST（= 2026-09-20T21:00Z）。
    catchup で 9/20 分は起動していない（`ActionCounts.Total` が 3 のまま）
  - `with-maintenance-pause.sh` の往復（pause+印 → `maintenance_in_progress` → 終了で unpause）を本番 Schedule で確認
  - `avp-daily-watchdog` を登録・trigger → 9/20 分に `DAILY_AUTOMATION_NOT_STARTED` を記録（事故の検知そのもの）。
    9/20 は Episode を作らない判断なので、運用者が `resolved_at` を手で入れた（記録は残す）
