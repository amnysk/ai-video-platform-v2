# ADR-0054: 日次 Workflow の失敗検知（Episode が作られない失敗を見逃さない）

## Status

Proposed (2026-10-21)

## Context

`avp-daily-episode` が起動した `DailyEpisodeWorkflow` が、Episode を作る前に数秒で
FAILED になる事故が実際に起きた（7日連続、各約9.5秒、Episode も slot も無し）。
当時の watchdog（ADR-0027 / ADR-0031）はどれも検知できなかった。

- `DAILY_AUTOMATION_NOT_STARTED` は「workflow が1つも**始まっていない**」ときだけ。
  失敗した workflow も「始まった」ので健全扱いになる（`has_workflow=True`）。
- `EPISODE_*` / `PIPELINE_OUTCOME_MISMATCH` は Episode 単位。Episode が無ければ対象が無い。
- PR #8 の日次異常レポートは `workflow_failure` / `missed_schedule_run` を出すが、
  読み取り専用の事後レポートで `operational_anomalies` に書かない（通知・release preflight の
  入口にならない）。

つまり「Episode が作られる前の失敗」には**検出する主体が無かった**。

## Decision

**watchdog が、直近 N 日（既定3日）の各 slot 日について日次 workflow の終了状態を読み、
失敗で閉じたのに Episode（slot）が無い日を `DAILY_WORKFLOW_FAILED` として記録する。**
読めなかった日は `DAILY_WORKFLOW_UNOBSERVABLE` として記録し、黙って健全にしない。

1. **判定は純粋関数**（`domain/daily_failure.py`）。入力は「その日に slot があるか」「その日に
   始まった実行の一覧（状態・失敗要約）」「読み取りが成功したか」「最新の slot 日か」。
   - slot がある → OK（Episode 単位の検査が担当）
   - 読み取り失敗 → UNOBSERVABLE
   - 実行が無い: 最新 slot 日は `DAILY_AUTOMATION_NOT_STARTED` の担当なので保留、
     過去日は保持期間で消えた可能性があるので UNOBSERVABLE
   - Completed / Running / ContinuedAsNew が1つでもある → OK（再実行が成功・進行中なら失敗日ではない）
   - それ以外で Failed / TimedOut / Terminated / Canceled がある → FAILED
   - 未知の状態だけ → UNOBSERVABLE
2. **読み取りは Protocol の背後**（`DailyRunReader`）。実装
   （`infrastructure/temporal/daily_runs.py`）は Temporal visibility
   （`list_workflows`）を第一に、失敗したら Schedule の `recent_actions` → 各 workflow の
   `describe` を第二に使う。両方失敗したら例外 → その日々は UNOBSERVABLE。
3. **記録は既存の `operational_anomalies`**（Schedule 系 = `episode_id IS NULL`、
   `(kind, anomaly_date)` で1日1行）。`anomaly_date` = slot 日（運用 TZ）。
   したがって hourly 再実行は同じ行の `occurrences` を増やすだけで行は増えず
   （`is_new=False` なので「新規」として再通知されない）、窓の外の日付は新規に作らない。
4. **連続失敗数**は `detail.consecutive_failures`。窓内は純粋関数で数え、窓の直前の日が
   未解決の FAILED 行として残っていればその値から続ける（保持期間・窓を超えても
   「何日連続か」が切れない）。UNOBSERVABLE は連続を切る（確認できた失敗だけを数える）。
5. **自動 resolve**: slot が現れた日、または窓内でその日が OK 判定になった日は、既存の
   `resolve`（`resolved_at` を埋める。再度失敗に見えれば `record` が再オープンして再通知）。
   窓の外の未解決行は、slot が現れたときだけ閉じる（読めない日を勝手に OK にしない）。
   既に FAILED 行がある日が、保持期間切れで UNOBSERVABLE に変わっても、FAILED 行は
   上書き・解決せず、UNOBSERVABLE 行も作らない（確定した事実を劣化させない）。
6. **detail に secret を入れない**（INV-20 / INV-39）。失敗の型名と message だけを入れ、
   `domain/daily_failure.py::summarize_failure` が token / key / Bearer / URL query / userinfo /
   長い不透明文字列 / UUID を伏せ、空白を畳み、型 80 文字・message 200 文字に切る。
   stack trace・入力 payload は取らない。
7. `WatchdogCheckRequest.daily_failure_lookback_days`（既定3、上限7）。既定値の宣言元は
   `contracts/schedule_guard.py`。
8. watchdog は検出・記録だけで何も再実行しない。

## Alternatives

**(a) PR #8 の日次レポートに通知を足す** — レポートは読み取り専用（architecture test で固定）で、
`operational_anomalies` を書かない契約。却下。

**(b) 失敗した workflow 自身が失敗時に anomaly を書く** — workflow がクラッシュ・terminate
されると書けない（今回の「数秒で失敗」は書ける場合もあるが、terminate / timeout は書けない）。
外部からの観測が必要。却下。

**(c) UNOBSERVABLE を作らず、読めなければ何もしない** — 保持期間切れや Temporal 障害で
「見えていない」ことが「健全」と区別できなくなる。2026-09 の事故の教訓（検知漏れ）と逆。却下。

**(d) 失敗を `DAILY_AUTOMATION_NOT_STARTED` に統合する** — 意味が違う（動かなかった vs
動いて失敗した）。対処も違う（worker 起動 vs ログ/原因調査）。連続日数や原因要約も持てない。却下。

## Consequences

**良い側**
- Episode が作られない失敗が、最初の watchdog（最大1時間後）で `operational_anomalies` に残り、
  既存の通知経路（`AnomalyNotifier`）に乗る
- 「N 日連続」が detail で見える
- 読めないことが `DAILY_WORKFLOW_UNOBSERVABLE` として残り、健全と区別できる

**悪い側 / 引き受けた負債**
- AnomalyKind が2つ増えるため migration 0017 が必要（`kind` の CHECK 制約の拡張のみ。
  expand-only。列・表は増えない）
- 保持期間（既定1日）を過ぎた過去日の失敗は原因が取れない。履歴が消えていても、
  記録済みの行は残る。初めて見る古い日は UNOBSERVABLE になる
- 過去日で「正常に Completed したが slot が無い（pause 等で skip）」日も、保持期間が切れていれば
  UNOBSERVABLE になる（窓を既定3日に限り、誤報を数日以内に抑える）
- 失敗理由の取得は `handle.result()` で行う。取れなければ型・message は空のまま FAILED を記録する
- slot 日は「実行の start_time を運用 TZ に直した日付」で決める。予定時刻から大きく遅れて
  始まった実行は別の日に数えられうる
