# テスト設計の根拠: 日次 workflow の失敗検知（ADR-0054 / INV-68）

事故: 日次 workflow が Episode を作る前に約9.5秒で FAILED し、7日連続で `operational_anomalies` が空だった。
`DAILY_AUTOMATION_NOT_STARTED` は「始まっていない」だけ、`EPISODE_*` は Episode が要るため、どちらにも映らない。
落ちたら実装を直す（テストを緩めない）。

## 純粋ロジック（`tests/unit/test_daily_failure_domain.py`）

| テスト | 止めるもの |
|---|---|
| `test_failed_without_slot_is_failed` | Failed / TimedOut / Terminated / Canceled のどれで閉じても失敗日になる（terminate や timeout を見落とさない） |
| `test_completed_or_running_is_ok` | 成功・進行中を失敗にしない。失敗後に成功した再実行がある日は失敗日ではない（誤報で信頼を失わない） |
| `test_slot_means_episode_checks_own_the_day` | slot（= Episode）がある日を二重報告しない |
| `test_latest_failed_run_is_the_one_reported` | 同じ日の複数失敗で、最後の失敗を原因として出す |
| `test_unreadable_is_unobservable_never_ok` | 読めなかったことを健全にしない（INV-68 の核） |
| `test_no_run_found_defers_*` | 最新日は NOT_STARTED に委ね、古い日は保持期間切れとして UNOBSERVABLE（二重報告と黙殺の両方を避ける） |
| `test_unknown_status_only_is_unobservable` | 未知の状態を OK に倒さない |
| `test_lookback_window_*` / `test_streaks_*` | 窓の境界（最古日を含む）、上限の丸め、連続数・窓直前からの継続・健全日での切れ目 |
| `test_slot_date_uses_the_operating_timezone` | UTC 日付との取り違え（06:00 JST は前日 21:00 UTC） |
| `test_summary_redacts_secrets_and_truncates` / `test_summary_of_nothing_is_nothing` | api_key / Bearer / URL の userinfo と query / UUID / 長い不透明文字列を伏せ、型 80・message 200 文字に切る。原因が無いときに捏造しない |

## watchdog の配線（`tests/unit/test_daily_workflow_failure_watchdog.py`、fake reader + 実 DB スキーマ）

| テスト | 止めるもの |
|---|---|
| `test_failed_yesterday_and_today_records_two_consecutive_days` | 事故そのもの。昨日・今日の失敗が2行になり `consecutive_failures` が 1, 2、結果に連続数が出る |
| `test_completed_running_and_episode_present_record_nothing` | 誤報。成功・実行中・Episode ありは何も残さない |
| `test_terminated_and_timed_out_are_recorded_with_their_own_status` | 状態の区別が detail に残る。取れない原因を捏造しない |
| `test_rerun_adds_no_rows_and_is_not_new` | hourly 再実行で行・通知が増えない。既記録の日を「新規」として再発報しない |
| `test_unreadable_temporal_*` / `test_retention_expired_*` | 読めない・履歴切れが UNOBSERVABLE として残る。最新日は NOT_STARTED の担当 |
| `test_recorded_failure_survives_retention_expiry` | 確定した FAILED が、履歴が消えた翌時間に UNOBSERVABLE へ劣化・解決されない |
| `test_failure_outside_window_is_not_recorded` | 窓の外の古い失敗を新規にしない（読みもしない） |
| `test_window_boundary_*` | 窓の直前に記録済みの連続数から続ける（保持期間を超えても「何日連続」が切れない） |
| `test_days_before_the_first_slot_are_not_judged` | 日次運用の開始前の日、新品 DB で誤報しない |
| `test_failure_resolves_*` / `test_old_open_failure_outside_window_*` | 既存の anomaly lifecycle と同じ自動 resolve。窓外の行は slot が現れたときだけ閉じる |
| `test_detail_never_carries_secrets_even_from_a_careless_reader` | reader が生の message を返しても DB に secret が入らない（二重の防御） |

## Temporal reader（`tests/unit/test_daily_runs_reader.py`、fake client）

visibility 第一・Schedule 第二・両方失敗で例外（= UNOBSERVABLE の根拠）、Temporal の全 status のマッピング、
失敗原因が cause 連鎖の末端から要約されること、期限切れ実行の skip。実 Temporal・本番には接続しない。

## 既存テストの変更

`tests/unit/test_daily_watchdog.py` と `tests/integration/test_incident_recovery_e2e.py` は、
`run_daily_watchdog` に必須引数 `daily_run_reader` が増えた分の呼び出し側の追従だけ（期待値は変えていない。
fake は「実行が見つからない」を返し、既存の検査に影響しない）。

## 未検証

実 Temporal に対する `TemporalDailyRunReader` の挙動（visibility の状態名・`handle.result()` の例外型）は
fake でしか確かめていない。本番へは接続しない方針のため、隔離した namespace での統合確認が別途必要。
