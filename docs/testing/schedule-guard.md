# テスト設計の根拠: Daily Schedule ガード / watchdog（ADR-0027 / ADR-0031）

各テストが何の事故・退行を止めるために存在するか。落ちたら実装を直す（テストを緩めない）。

| テスト | 止めるもの |
|---|---|
| `tests/unit/test_schedule_guard_domain.py` | maintenance と emergency の分類。壊れた・偽造された印を emergency 側へ倒す（自動解除しない方向が安全）。日次の予定時刻・slot_date が運用タイムゾーンで決まること（UTC 日付との取り違え） |
| `tests/unit/test_schedule_guard.py`（Test B, C） | deploy 後に running へ戻ること、解除後に describe で再確認すること、運用者の pause を begin / end / reconcile が触らないこと、TTL 切れの解除、冪等性 |
| `tests/unit/test_with_maintenance_pause.py`（Test B2） | deploy 失敗・中断（SIGTERM）でも end が呼ばれること、begin exit 3 で解除しないこと、begin 失敗でコマンドを実行しないこと、end 失敗を隠さないこと |
| `tests/unit/test_daily_watchdog.py`（Test D） | pause のままの検知（今回の事故）、slot 不在の `DAILY_AUTOMATION_NOT_STARTED`、slot / workflow があれば健全、猶予内は判定しない、1日1回の通知、通知失敗の再送、visibility 障害で未起動を隠さない、emergency pause を解除しない |
| `tests/unit/test_operational_anomalies.py` | 異常の1日1行（一意制約）、再発時の再オープンと再通知、DB の kind CHECK |
| `tests/unit/test_daily_watchdog_workflow.py` | workflow が決定論の時刻を Activity へ渡すこと。Activity が Temporal client のある worker にだけ登録されること |
| `tests/unit/test_schedule_registration.py` | watchdog Schedule の定義（別 id・毎時・SKIP）。`ensure-daily-schedule --apply` が既存の emergency pause を外さないこと（従来は外しえた） |
| `tests/architecture/test_schedule_guard_boundaries.py` | `unpause` を呼べる場所をガード・Schedule 実装・運用者の CLI に限定する（emergency pause を外す経路を増やさない） |
| `tests/integration/test_schedule_guard_temporal.py` | fake では言い切れない実 Temporal の挙動（pause note が describe に出る・update が pause を保つ・begin/end/reconcile が emergency を触らない）。本番 namespace では動かさない |
| `tests/contract/test_migration_matches_models.py`（既存） | migration 0010 とモデルの一致 |

## 追加: deploy の配線（tests/contract/test_deploy_workers.py）

`test_makefile_wraps_the_deploy_in_a_maintenance_pause`: `make deploy-workers` が素の
`deploy-workers.sh` ではなく `with-maintenance-pause.sh`（EXIT trap で unpause + describe 確認）経由であること、
guard を動かす python を `PYTHON=` で差し替えられること。wrapper 単体は正しくても、Makefile が
それを通さなければ「pause したまま deploy が落ちる」経路が残る（2026-09-19 の事故の形）ため、配線そのものを固定する。

## 追加: インシデント 8fb66fcb の再検証（`tests/unit/test_daily_watchdog.py::test_incident_8fb66fcb_*`）

2026-09-21〜09-27 の7日間、日次 Schedule は始まり続けたのに個々の Episode が `blocked` のまま
7日間気づかれなかった事故（`docs/decisions/0031-daily-watchdog-progress-completion-upload.md`
§検証記録）の再検証として追加。既存のADR-0031テスト群は各検査条件を個別の合成データで
確認しているが、以下は**この事故の実タイムライン・実日付・production の既定値（override無し）
だけ**を使って再現し、「ADR-0031の実装そのものに検知漏れがあるのか、単に本番未デプロイ
だっただけなのか」を機械的に切り分けるために存在する。

| テスト | 止めるもの |
|---|---|
| `test_incident_8fb66fcb_a_blocked_episode_is_caught_within_the_first_hourly_check` | 09-21 06:00 作成→06:20 blocked のタイムラインで、最初の毎時検査（06:35）が override無しの既定値だけで検知すること。「日次自動化は始まった」（旧・本番の判定基準では健全）ことと「個々のEpisodeがblocked」であることが独立した検査であることも合わせて固定する |
| `test_incident_8fb66fcb_the_same_blocked_episode_is_renotified_every_day_for_seven_days` | 放置されたまま7日間経っても、「初日にログを1回出して終わり」にならず、日ごとに新しい行が立って毎日再通知され続けること（`anomaly_date` が日次で変わる一意制約の帰結が壊れていないこと） |
| `test_incident_8fb66fcb_completion_and_upload_deadlines_also_fire_with_pure_defaults` | 進行（stall）だけでなく完成・投稿の期限超過も、override無しの production 既定値だけで独立に検知される多層防御が壊れていないこと |

これら3件は現在の実装に対して**追加時点で green**（実装を直していない）。目的は退行検知であり、
将来ここを触って壊せば実際の事故タイムラインの検知が失敗する形で気づけるようにするため。

## 既知の課題（この変更では直していない）

- `tests/integration/test_pipeline_schedule.py` は Temporal を `localhost:7233`（本番の `default` namespace）へ直書きしており、
  実行すると本番 Temporal に接続する。integration テスト全体の namespace 隔離（`TEST_TEMPORAL_NAMESPACE` 等）は
  別ブランチ（`claude/topic-planner` の作業ツリーにある未コミットの WIP）が扱っている。この統合の検証では、
  Temporal / Postgres / MinIO を別ポートに立てた使い捨て環境で実行した。
