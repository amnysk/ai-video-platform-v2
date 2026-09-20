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

`operational_anomalies`（1日1行）と ERROR ログ `OPERATIONAL_ANOMALY anomaly=<KIND> date=...`。

| kind | 意味 | 対処 |
|---|---|---|
| `DAILY_AUTOMATION_NOT_STARTED` | 予定 + 猶予を過ぎても slot も DailyEpisodeWorkflow も無い | `status` で Schedule と worker を確認。直ったら回復時に自動で閉じる |
| `SCHEDULE_PAUSED_UNEXPECTEDLY` | 印の無い pause が続いている | 意図した停止なら放置（記録は続く）。そうでなければ `--unpause` |
| `SCHEDULE_MAINTENANCE_OVERRUN` | maintenance pause が期限を過ぎ、ガードが解除した | deploy が `end` を呼べなかった。deploy 手順を確認 |
| `SCHEDULE_MISSING` | Schedule が存在しない | `ensure-daily-schedule.py --apply` |
| `SCHEDULE_NEXT_RUN_INVALID` | 動いているが次回実行が無い・遠い | describe を確認（cron・タイムゾーン） |

```sql
select kind, anomaly_date, occurrences, first_detected_at, resolved_at, notified_at
from operational_anomalies order by anomaly_date desc, kind;
```

通知は `AnomalyNotifier`（`infrastructure/observability/anomaly_notifier.py`）。既定はログのみ。
Slack / メール等は同じ Protocol を実装して `workers/pipeline/activities.py` で差し替える。

## 6. 制限

- Temporal 自体が止まると watchdog も動かない。ホストの cron 等から `schedule-guard.py status`（exit 1 で異常）を
  呼ぶと別系統の監視になる
- watchdog が判定できる daily の cron は `M H * * *` の形だけ。それ以外は `unsupported_cron` で判定しない
- 検査は毎時 :35（JST）。始まらなかった日の検知は最長で予定 + 猶予 + 1 時間

## `make deploy-workers` との配線

`make deploy-workers [PYTHON=.venv/bin/python]` は `scripts/with-maintenance-pause.sh --reason deploy-workers --ttl 45m`
で `scripts/deploy-workers.sh` を包む。成功・失敗・Ctrl-C のどれでも EXIT trap が `maintenance end`
（unpause → `paused=false` と次回実行が未来であることを describe で確認）を実行する。
Schedule が**運用者の pause（印なし）**のときは deploy だけ実行し、解除はしない（緊急停止を deploy が外さない）。
