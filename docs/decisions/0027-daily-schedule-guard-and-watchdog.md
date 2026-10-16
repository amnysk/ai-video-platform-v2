# ADR-0027: Daily Schedule の望ましい状態・maintenance pause・watchdog

## Status

Accepted (2026-09-20)

## Context

2026-09-19 04:21 頃、topic-planner の deploy のため Temporal Schedule `avp-daily-episode` を pause したが、
解除されないまま 2026-09-20 06:00 を迎え、DailyEpisodeWorkflow が起動しなかった（`Paused=true`）。
誰にも検知されなかった。原因は次の3点。

1. 「本番では Schedule は動いている」という望ましい状態がどこにも定義されていない
2. deploy 手順の pause と解除が別々の手作業で、途中で失敗・中断すると解除が漏れる
3. 「今日の自動運転が始まったか」を確かめる仕組みが無い（Schedule 自体が止まると何も鳴らない）

制約: INV-2（スケジューラは Schedule / workflow start だけ）、INV-5（プロセス内の状態ポーリングを作らない）、
運用者の**意図的な緊急停止を自動で無効化しない**。

## Decision

### 1. 望ましい状態を1箇所に置く（`contracts/schedule_guard.py`）

`DESIRED_DAILY_SCHEDULE_PAUSED = False`。本番の `avp-daily-episode` は paused=false が原則。
語彙（`ScheduleHealth` / `AnomalyKind` / 定数）もここが唯一の宣言元。

### 2. pause は2種類に分け、印は Schedule の pause note に置く

- **maintenance pause**: ガードが作る。note が `AVP-MAINTENANCE/1 {"reason","deadline","owner"}`。deadline は
  UTC の期限（TTL 既定 30 分・上限 6 時間）。**ガードだけが解除してよい**
- **emergency pause**: 上記の印を持たないすべての pause（`temporal schedule toggle --pause`、
  `ensure-daily-schedule.py --pause`、手動）。**begin / end / reconcile のどれも触らない**
- 印が壊れている・期限が読めない・接頭辞だけ、も emergency 側に倒す（安全側）

印の置き場は **Temporal の pause note**（DB テーブルではない）。理由: pause の状態と印が同じ原子的な
更新で動く（DB と Temporal の食い違いが起きない）。運用者が pause し直せば note が置き換わり、自動的に
emergency として扱われる（deploy の後始末に解除されない）。DB スイッチ（`PAUSED` / `UPLOADS_PAUSED`、
ADR-0021）は別の仕組みで、そのまま残す。status と異常の detail に併記する。

### 3. ガード CLI とラッパー（`scripts/schedule-guard.py`、`scripts/with-maintenance-pause.sh`）

`maintenance begin --reason R --ttl 45m` / `maintenance end` / `reconcile` / `status [--json]`。

- begin: 運用者の pause（印なし）なら**何もせず exit 3**。Schedule が無ければ exit 1
- end: 印のある pause を unpause し、describe で「paused=false・印なし・次回実行が 25 時間以内の未来」を
  確認する。満たさなければ exit 1。運用者の pause なら exit 3（解除しない）。既に動いていれば冪等に成功
- reconcile: **期限切れの maintenance pause だけ**を解除する（冪等）
- ラッパーは `begin → コマンド → trap EXIT で end`。コマンドの失敗・Ctrl-C・SIGTERM でも end を呼ぶ。
  begin が exit 3 ならコマンドは実行するが end は呼ばない。begin が失敗ならコマンドを実行しない。
  end が失敗したら（コマンドが成功でも）非0で終わる
- deploy が失敗しても Schedule は解除する。worker が落ちていても Temporal の task queue は仕事を保持し、
  worker が戻れば進む。paused のまま黙って翌日を迎えるより安全

### 4. Daily watchdog（別 Schedule `avp-daily-watchdog`、毎時 :35 JST）

`DailyWatchdogWorkflow`（queue `pipeline`）→ Activity `pipeline_watchdog_check`
（`infrastructure/temporal/watchdog.py`）。daily と別の Schedule なので、daily が pause されていても動く。

1. reconcile（期限切れの maintenance pause の解除。`SCHEDULE_MAINTENANCE_OVERRUN` を記録）
2. Schedule の分類を記録（`SCHEDULE_PAUSED_UNEXPECTEDLY` / `SCHEDULE_MISSING` / `SCHEDULE_NEXT_RUN_INVALID`）
3. 直近の予定時刻（cron から導出。`M H * * *` の日次のみ対応）+ 猶予（既定 30 分）を過ぎて、その日の
   `daily_episode_slots` も、その時刻以降に始まった `DailyEpisodeWorkflow`（visibility）も無ければ
   **`DAILY_AUTOMATION_NOT_STARTED`**
4. 異常は `operational_anomalies`（migration 0010、`(kind, anomaly_date)` 一意）に1日1行で記録し、
   ERROR ログ `OPERATIONAL_ANOMALY anomaly=<KIND> ...` を出す。通知は `AnomalyNotifier` Protocol
   （既定はログのみ）。通知に失敗した行は `notified_at` が空のまま次回再送される
5. 回復したら（slot ができた・Schedule が健全）open な異常を閉じる

watchdog は emergency pause を**解除しない**。検知して記録するだけ。

### 5. `ensure-daily-schedule.py --apply` は既存の pause を外さない

Schedule の定義更新（`update`）は、明示的に `--paused` を指定しない限り現在の state（paused と note）を保つ。
従来は `paused=False` の定義で上書きしており、`--apply` が emergency pause を黙って解除しえた。

## Alternatives

- **DB に「maintenance 中」を持つ**: Temporal の pause と2か所になり、片方だけ更新される事故の型。採らない
- **watchdog が pause を常に解除する**: 意図的な緊急停止を無効化する。採らない
- **プロセス内タイマーで Schedule を監視**: INV-2 / INV-5 違反。別 Schedule + workflow にする
- **deploy 失敗時は pause のまま残して人に知らせる**: 今回の事故そのもの（知らせが届かなければ翌日が消える）
- **cron の一般解釈（croniter 等）**: INV-2 が cron ライブラリを禁じる。日次 `M H * * *` だけ解釈し、
  それ以外は `unsupported_cron`（判定しない）と明示する
- **外部通知サービスの導入**: 本 ADR の範囲外。`AnomalyNotifier` で差し込める形だけ用意した

## Consequences

- 良い: pause 忘れは TTL（最大 6 時間）と watchdog で自動的に戻る。手動停止は戻らない
- 良い: 「今日始まらなかった」は最長 1 時間（毎時の検査）で `operational_anomalies` に残る
- 悪い: Temporal 自体が止まっている場合、watchdog も動かない（別系統の監視は将来課題。`schedule-guard.py status`
  をホストの cron 等から呼べる）
- 悪い: maintenance の TTL を超える deploy は、途中で watchdog が解除しうる。`--ttl` を作業時間に合わせる
- 悪い: watchdog Schedule の登録と migration 0010 の適用が前提（`docs/operations/daily-schedule-guard.md`）
- 悪い: 毎時の検査は Activity が DB と Temporal を読むため pipeline worker が Temporal client を持つ
