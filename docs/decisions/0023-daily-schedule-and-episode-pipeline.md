# ADR-0023: Daily Schedule と Episode pipeline workflow

## Status

Accepted (2026-09-15)

## Context

Phase 1-6 で Script → Storyboard → Production → Render → Upload の工程は揃ったが、起動は人間の API 呼び出し
（`apps/api/workflow_starter.py`）だけだった。毎日 1 本を自動で作り、`render_ready` に来たら private 投稿まで
進めたい。制約:

- INV-2: スケジューラは Temporal の Schedule / workflow start だけを行う。cron ループをプロセス内に持たない
- INV-3 / INV-4 / INV-5: 工程の順序は Temporal workflow だけが持ち、worker 同士は import しない
- INV-13: Episode 間に暗黙の直列依存を作らない（ある日の Episode が blocked でも翌日は進む）
- 同じ日の trigger が重複・再実行・クラッシュしても 1 日の上限を超えて生成しない
- Shorts を前提にしない（`render_profile_id` を運ぶ）

## Decision

1. **Temporal Schedule `avp-daily-episode`** が `DailyEpisodeWorkflow`（queue `pipeline`、id 接頭辞
   `daily-episode`）を起動する。overlap は SKIP、catchup window は 1 時間。登録は
   `infrastructure/temporal/schedules.py::ensure_daily_episode_schedule`（create-or-update、冪等）と
   `scripts/ensure-daily-schedule.py`（既定 dry-run）
2. **DailyEpisodeWorkflow**: `slot_date` は Schedule の予定時刻（`TemporalScheduledStartTime`、無ければ
   workflow 開始時刻）を入力の IANA タイムゾーン（既定 `Asia/Tokyo`）で日付にしたもの。入力で上書き可。
   `pipeline_check_paused`（PAUSED）→ `pipeline_claim_daily_slot`（trigger id = workflow id、
   `DailyEpisodeSlotRepository.claim` / ADR-0021）→ 子 `EpisodePipelineWorkflow`
   （id `episode-{id}-pipeline`、ParentClosePolicy.ABANDON、id reuse ALLOW_DUPLICATE_FAILED_ONLY）を
   **起動するだけ**で終わる。すでに起動済みなら `already_started` を返す（二重起動しない）
   - 同じ trigger の再実行 / commit 後の Activity 再試行 → claim は EXISTING → 子は already started
   - 子を起動する前に落ちた trigger の Episode（`planned` のまま）→ 次の trigger が RESUME で拾う
3. **EpisodePipelineWorkflow**: 工程を名前と task queue で `execute_child_workflow` する。workflow id は
   API と同じ規約（`contracts/pipeline.py` に純粋関数、API 側と一致をテストで固定）なので、API と pipeline が
   同じ工程を同時に走らせることは Temporal が拒否する。子が駐機点
   （script_ready / storyboard_ready / assets_ready / render_ready / uploaded）以外を返す・失敗する・
   同じ id が走っている → そこで `stopped` を返して終わる（ループしない）。子は ABANDON（pipeline を
   止めても有料工程・投稿を途中で殺さない）
4. **投稿ゲート** `pipeline_upload_gate`（Upload の直前）: PAUSED / UPLOADS_PAUSED（env OR DB switch）、
   Episode が `render_ready` でない、現行 `final_video` が無い、`youtube_upload` 予約に spent または
   dispatched のものがある → 拒否して `upload_skipped`。sha の照合は UploadWorkflow 自身が行う（既存）
5. 設定は `Settings` の `daily_episode_limit`（1）/ `daily_schedule_cron`（`0 6 * * *`）/
   `schedule_timezone` / `daily_schedule_id` / `pipeline_render_profile_id` / `paused`

## Alternatives

- **プロセス内 cron / APScheduler**: INV-2 違反。再起動・多重起動で重複する
- **Daily が pipeline の完了を待つ**: overlap SKIP が長い工程に引きずられ翌日の trigger を捨てる（INV-13 違反）
- **1 本の巨大 workflow に全工程を Activity で持つ**: 既存の工程 workflow（admit・失敗記録）を再実装することになる
- **Temporal の workflow id だけで日次の一意性を保つ**: 上限 N 本・RESUME を表せない。DB の slot を使う

## Consequences

- 工程 worker（script ... upload）と pipeline worker を並べて起動する必要がある（docs/operations/pipeline-worker.md）
- 止まった Episode の再開は従来どおり人間の API 呼び出し。pipeline は自動で再試行しない
- PAUSED は新しい Daily の起動と投稿ゲートを止める。すでに走っている工程は止めない
