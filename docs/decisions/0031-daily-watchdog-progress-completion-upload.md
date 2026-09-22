# ADR-0031: Daily Watchdog を起動・進行・完成・投稿の4段階で判定する

## Status

Accepted (2026-09-23)

## Context

ADR-0027 の watchdog（`infrastructure/temporal/watchdog.py::run_daily_watchdog`）は
「その日の daily が**始まったか**」（`daily_episode_slots` または `DailyEpisodeWorkflow` の存在）
しか見ていない。

2026-09-22 の事故はこれをすり抜けた: daily は正常に始まり、`EpisodePipelineWorkflow` は
Production の子 workflow が `assets_ready` ではなく `blocked` 相当の状態を返したところで
`_stop()`（`workers/pipeline/workflows.py:318-325`）を呼び、`result.outcome = PipelineOutcome.STOPPED`
を持ったまま**正常に complete した**。Temporal 上は `completed` であり、`DailyEpisodeWorkflow` は
子を `ParentClosePolicy.ABANDON` で起動して結果を待っていないため、この `outcome=stopped` を
誰も見ていない。watchdog は「今日は始まった」を確認して健全と判定した。

追加で判明した事実:

- Episode の状態遷移のたびに `state_changed_at` が更新される（`docs/domain/state-transitions.md` §実装規則4）。
  停滞検知の材料として使える。
- `blocked` は state-transitions.md 上「通報される（放置されない）」と書かれているが、
  それを実装する通知経路は watchdog 以外に存在しない（未検査）。
- Render・Upload 工程は POST での個別再開しかなく、pipeline workflow が終了した後は
  「今日の Episode が完成・投稿まで進んだか」を確認する仕組みが無い。
- `operational_anomalies` の一意制約は `(kind, anomaly_date)`。1日に複数の異なる Episode が
  それぞれ問題を起こした場合、この制約のままでは2件目が1件目を上書き（`occurrences` 加算）し、
  どの Episode が問題かの情報が失われうる。

## Decision

**(1) watchdog の判定を4段階に分ける。** `pipeline_watchdog_check` を拡張し、既存の「起動」
（INV-26、変更なし）に加えて:

- **進行**: `status IN (blocked, needs_work)` の Episode のうち、`state_changed_at` が
  工程ごとの停滞猶予（`contracts/schedule_guard.py` に宣言する
  `STAGE_STALL_GRACE_MINUTES`、既定値は工程非依存の単一値からスタートし、将来 Shorts 以外の
  尺・工程が増えたら工程別に分ける。**値をコードへ埋め込まず、この1箇所だけを参照する**）を
  過ぎていれば `EPISODE_STAGE_STALLED`
- **完成**: Episode の作成時刻から `COMPLETION_DEADLINE_HOURS`（既定値、同宣言元）を過ぎても
  `render_ready` / `approved` / `uploaded` / `analyzed` のいずれにも達していなければ
  `EPISODE_NOT_COMPLETED_BY_DEADLINE`
- **投稿**: `render_ready` / `approved` に到達済みで `UPLOAD_DEADLINE_HOURS`（同宣言元）を過ぎても
  `upload_receipt` と YouTube video id が確定していなければ `EPISODE_NOT_UPLOADED_BY_DEADLINE`。
  ただし運用スイッチ `UPLOADS_PAUSED` が有効な間は、これは**意図した停止**として anomaly を作らない
  （既存スイッチの流用。新設しない）
- **整合性**: 今日 close した `EpisodePipelineWorkflow` 実行のうち、型付き結果の
  `outcome == PipelineOutcome.STOPPED` なのに Episode の DB 状態が非終端の「問題なし」に見える
  （＝上記いずれのチェックにも引っかからない）組み合わせがあれば `PIPELINE_OUTCOME_MISMATCH`
  として記録する。「Temporal completed を成功と見なさない」ことの直接の機械検査になる。
  **回復判定は他の3種と異なる**（独立レビューで判明・修正）: 検出範囲が「今日 local midnight
  以降に close した実行」なので、翌日以降は同じ行を再検出できず「今回もいる/いない」の単純な
  差分では閉じられない。そのため回復は「今この時点の Episode の状態」で判定する: 今回いずれかの
  他チェックに covered された（DB 側が追いついた）か、既に完成状態（`render_ready` 以降）に
  達していれば閉じる。この非対称性を踏まえず実装すると、`PIPELINE_OUTCOME_MISMATCH` だけが
  永久に開いたまま残る（初回実装の欠陥だった）

すべて工程・尺（Shorts 等）に固有の値をハードコードしない。既定値は `contracts/schedule_guard.py`
の定数として単一宣言し、`infrastructure/config.py` の Settings から上書き可能にする。

**(2) `operational_anomalies` に Episode 単位の粒度を持たせる。** nullable `episode_id UUID` 列を
追加する migration を書く。一意制約を2本の部分インデックスに分ける:

- `UNIQUE (kind, anomaly_date) WHERE episode_id IS NULL` — 既存のSchedule/daily-start系（変更なし）
- `UNIQUE (kind, anomaly_date, episode_id) WHERE episode_id IS NOT NULL` — 新設のEpisode単位の
  異常。同日に複数のEpisodeがそれぞれ問題を起こしても、Episodeごとに1行ずつ残る

`AnomalyKind` は `contracts/schedule_guard.py` の閉じた列挙のまま拡張する（CHECK制約も migration で
更新。AGENTS §8 の単一宣言元を維持）。

**(3) 通知未設定を診断で明示する。** `schedule-guard.py status --json` の出力に
`"notifier": "log_only" | "configured"` を足す。判定は `AnomalyNotifier` の実装クラスが
既定の `LoggingAnomalyNotifier` かどうかで決める（新しい設定フラグを増やさない）。
ログ出力の成功を「通知済み」と扱う既存の意味論（§Consequences で明記）は変えない
（ログそのものが運用上の通知経路である前提は ADR-0027 のまま）が、**それが唯一の経路であることを
診断で可視化する**。

**(4) 監視結果の内容。** 各 anomaly の `detail` JSON に `episode_id` / `stopped_stage or current_status` /
`reason` / `resumable`（ADR-0032 の dry-run 判定関数をそのまま呼んで真偽値を入れる。判定ロジックを
二重化しない）を含める。

watchdog は検出・記録するだけで、Episode の状態を変更しない（既存方針の継続。ADR-0027 §4末尾と同じ）。

## Alternatives

**(a) Episode ごとに別の watchdog 種別を新設する** — 「既存の watchdog を拡張する」という
指示と衝突し、同種の監視機構が2つできる。却下。

**(b) `operational_anomalies` の一意制約を `(kind, anomaly_date, episode_id)` 一本に統一し
episode_id に固定のダミー値を入れる** — 部分インデックス2本より単純に見えるが、
「schedule 系の異常に架空の episode_id を割り当てる」ことになり、`detail` を読む運用者が混乱する。
部分インデックスの方が意味が明確。却下。

**(c) `PIPELINE_OUTCOME_MISMATCH` を作らず、既存のEpisode状態ベースの検査だけに頼る** — 実装は
簡単になるが、「Temporalのcompletedだけで成功判定しない」という指示を機械検査として持たない。
DB側の見落としに対する二重の安全網として残す。採用（却下しない）。

## Consequences

**良い側**
- 2026-09-22 型の事故（Production 停止 → Render/Upload 未到達）は、始まりから最長
  `STAGE_STALL_GRACE_MINUTES` で `EPISODE_STAGE_STALLED` として検出される（翌朝レポートを待たない）
- 完成・投稿それぞれの期限超過が独立に検出でき、意図した pause（`UPLOADS_PAUSED`）と区別される
- 同日に複数 Episode が問題を起こしても取りこぼさない

**悪い側 / 引き受けた負債**
- 新しい4種の `AnomalyKind` 分だけ CHECK 制約の migration が増える（意図した設計）
- `STAGE_STALL_GRACE_MINUTES` 等の既定値は経験則であり、実際の生成時間分布に基づく調整が今後要る
- `notifier: log_only` の可視化は「ログが通知として機能していない」ことの証明にはならない
  （ログを実際に監視しているかは運用側の責任として残る）
- `PIPELINE_OUTCOME_MISMATCH` は today closed な pipeline workflow の Temporal 履歴を読む必要があり、
  watchdog Activity が Temporal client を持つという ADR-0027 の既存の負債がそのまま続く
- migration 0012 の `downgrade()` は episode 単位の異常行を消してから旧 CHECK/UNIQUE に戻す
  （独立レビューが「消さずに戻すと CHECK 違反で downgrade 自体が失敗し、スキーマが壊れたまま
  残る」ことを実際に再現して発見・修正）。episode 単位の異常履歴は downgrade で失われる
  （監視の記録であり業務データではないため許容する）

## 機械検査

- `tests/unit/test_daily_watchdog.py`（新規: 4段階それぞれの検出条件・UPLOADS_PAUSED除外・
  同日複数Episodeの取りこぼし無し・PIPELINE_OUTCOME_MISMATCH）
- `tests/integration/test_pipeline_schedule.py`（新規: completed+outcome=stopped が healthy と
  判定されないこと）
- `tests/contract/test_operational_anomalies_episode_scope.py`（新規: 部分インデックス2本の制約）
