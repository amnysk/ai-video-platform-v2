# 不変条件（Invariants）

このファイルは**契約**であり、要約ではない。ここに書かれた条件を破る変更は
ADRなしにマージしてはならない（[AGENTS.md §6](../AGENTS.md)）。

- 接頭辞 `INV-` は全体で1系列。**番号は再利用しない。**
- 廃止した条件は削除せず「廃止（日付・理由）」を残す。
- 各条件には「機械検査」欄がある。`未検査` と書かれた条件は
  **願望であって保証ではない**。その上に別の設計を積まないこと。

## A. 呼び出し方向（結合）

### INV-1 UIはWorkerを直接呼ばない
Next.js / ブラウザから `workers/` のコードや Temporal task queue へ直接届く経路を作らない。
UIの入口は FastAPI のHTTP APIだけ。
**機械検査**: `tests/architecture/test_api_no_heavy_work.py::test_api_does_not_import_heavy_or_worker_modules`

### INV-2 SchedulerはWorkerを直接呼ばない
cron・timer 等のスケジューラは Temporal の schedule/workflow start だけを行い、
Activity や worker 関数を直接呼ばない。
**機械検査**: `tests/architecture/test_pipeline_scheduling.py`（Schedule を作るのは `infrastructure/temporal/schedules.py` だけ・プロセス内 cron ライブラリ禁止 / ADR-0023）

### INV-3 Workerは他のWorkerを直接呼ばない
`workers/<a>/` から `workers/<b>/` へのimportを禁止する。共有したいロジックは
`domain/` か `infrastructure/` へ降ろす。
**機械検査**: `tests/architecture/test_layering.py::test_workers_do_not_import_other_workers`

### INV-4 Workerは次のJobを決めない
Workerは与えられたJobを実行して結果を返すだけ。次に何を実行するかの判断は
Workflow定義（Temporal）のみが持つ。Worker内に「成功したら次は◯◯」を書かない。
**機械検査**: 未検査（レビュー項目）

### INV-5 TemporalがWorkflow実行を担当する
工程の順序・retry・タイムアウト・補償は Temporal workflow に表現する。
アプリ側に独自のスケジューリングループや状態ポーリングを作らない。
**機械検査**: 未検査

### INV-6 レイヤ依存は一方向
`apps/ → domain/, contracts/, infrastructure/`、
`workers/ → domain/, contracts/, infrastructure/`、
`infrastructure/ → domain/, contracts/`（ADR-0007 で追加）、
`domain/ → contracts/` のみ。
`domain/` は `apps/` `workers/` `infrastructure/` をimportせず、
DB・HTTP・Temporal・ファイルI/O にも触れない。
**機械検査**: `tests/architecture/test_layering.py::test_layer_dependencies_are_one_directional`
/ `::test_domain_has_no_io_dependencies`

## B. 状態と永続化

### INV-7 PostgreSQLがapplication stateのsource of truth
Episode / Job / Artifact のドメイン状態はPostgreSQLが権威。
Temporal・MinIO・UIキャッシュの値を権威として読まない。
**機械検査**: `tests/unit/test_api.py::test_episode_status_comes_from_the_database_not_the_workflow`

### INV-8 Temporal内部状態とapplication domain stateを分離する
workflow execution status（running/completed/terminated 等）を
Episode状態へ直接写像しない。両者は独立に進み、対応表を持たない。
UIに出すのはdomain state。
**機械検査**: `tests/unit/test_api.py::test_api_does_not_expose_temporal_internal_state`

### INV-9 MinIOがArtifact本体を保持する
バイナリ（動画・音声・画像）をPostgreSQLに入れない。DBが持つのは
参照（bucket/key/etag/size）とメタデータのみ。
**機械検査**: `tests/integration/test_minio_store.py`（本体が実MinIOに置かれ読み戻せること）
/ `scripts/smoke.sh`（Episode完了後の読み戻しとsha256照合）

### INV-10 Artifactはversionとschemaを持つ
**Phase 1 から有効**: すべてのArtifactは `artifact_type` と `schema_version` を持ち、
読み込み時にスキーマ検証を通る。スキーマ無しの成果物を作らない。
**Phase 2 から有効**（ADR-0012）: 同一論理成果物内での単調増加 `version` と
`superseded_at` による世代管理、および `input_hash` による再開判定。
content-addressed キー（`artifacts/{episode_id}/{artifact_type}/{sha256}.json`）と
`UNIQUE(episode_id, artifact_type, sha256)` は immutability の担保として残る。
非決定的な生成器（LLM）では sha256 が毎回変わるため、
**同一性の軸は `input_hash`** である。
**機械検査**: `tests/contract/test_artifact_schema.py` /
`tests/unit/test_artifact_generations.py`

### INV-11 Artifactはimmutable
一度書かれたArtifactオブジェクトは上書きしない。作り直しは新しい `version` を作る。
**機械検査**: `tests/unit/test_artifact_store.py::test_reput_with_different_content_raises_instead_of_overwriting`

## C. 失敗とretry

### INV-12 retry可能なJob失敗だけでEpisodeをterminal failedにしない
Jobの失敗は必ず [failure-policy](./failure-policy.md) の失敗クラスへ分類される。
`retryable` / `needs_input` クラスの失敗でEpisodeを `failed`（terminal）へ落とさない。
分類できない失敗は自動修復に流さず、人間の判断待ち（`blocked`）にする。
**機械検査**: `tests/unit/test_failure_policy.py` /
`tests/integration/test_episode_workflow.py::test_exhausted_retryable_failure_blocks_instead_of_failing`

### INV-13 1つのJobの失敗が他のEpisodeを止めない
Episode間に暗黙の直列依存を作らない。あるEpisodeの停止は
他のEpisodeのworkflow進行に影響しない。
**機械検査**: 未検査

### INV-14 Upload処理はidempotentである
**Phase 2 発効**（ADR-0010。upload工程が存在する時点から有効）。
同じ upload key（`episode_id` × `final_video` の sha256 × 投稿先。ADR-0020 §3。版・attempt を含めない）に
対するuploadは、何度実行しても最大1件のYouTube動画しか生成しない。冪等キーを予約台帳に永続化してから
外部呼び出しを行い、結果が読めないときは新しい session を開かない。
**機械検査**: 一部。upload key の決定性は
`tests/contract/test_upload_contracts.py::test_upload_key_excludes_attempt_and_time_and_depends_on_inputs`。
二重投稿が起きないこと（fake uploader の動画数）:
`tests/unit/test_upload_activities.py::test_concurrent_attempts_on_the_same_key_create_one_video`、
`tests/unit/test_upload_activities.py::test_crash_after_completion_before_the_id_is_saved_reconciles_by_status_query`、
`tests/unit/test_upload_activities.py::test_session_expired_after_bytes_without_marker_blocks_and_never_reopens`、
`tests/integration/test_upload_workflow_persistence.py::test_concurrent_double_start_and_concurrent_activities_make_one_video`。

### INV-15 課金を伴う外部呼び出しは予約を先に永続化する
provider呼び出しの前に予約レコードをcommitする。プロセスがクラッシュしても
未照合の予約が残り、**自動で再送も解放もしない**。
意味論と再開時の分岐は ADR-0013（予約台帳）が権威。
**機械検査**: `tests/unit/test_provider_reservations.py`

## D. 実行モデル

### INV-16 FastAPI上で重い処理を同期実行しない
API handlerは検証・永続化・workflow start/signal だけを行う。
動画生成・レンダリング・アップロード・外部AI呼び出しをリクエスト内で待たない。
**機械検査**: `tests/architecture/test_api_no_heavy_work.py`

### INV-17 Activityは冪等である
Temporalは同じActivityを複数回実行しうる。全Activityは再実行に耐えるか、
冪等キーで重複を吸収する。
**機械検査**: `tests/integration/test_episode_workflow.py::test_reexecuted_activity_does_not_corrupt_the_artifact`
/ `tests/unit/test_repositories.py::test_recording_the_same_artifact_twice_is_idempotent`

### INV-18 テストとCIから有料API・実投稿を呼ばない
外部provider（fal.ai / YouTube）へ到達しうるコードパスは、テストでは
必ずfake/adapterで置換する。**外部AIプロセス（Codex CLI）も対象**であり、
実呼び出しは `tests/live/` からのみ、環境変数 `AVP_LIVE_CODEX=1` を明示した場合に限る。
**機械検査**: `tests/architecture/test_no_live_calls.py`

## E. 外部副作用（前身repoから継承・非交渉）

### INV-19 YouTube投稿は private のみ
public/unlisted への自動切替、既存投稿の変更・削除・再投稿をしない。
公開は所有者の手動判断。
**機械検査**: 契約で private 以外を表現できないこと
`tests/contract/test_upload_contracts.py::test_privacy_status_cannot_be_anything_but_private`。
実際に送るメタデータが private であること
`tests/unit/test_upload_activities.py::test_upload_creates_one_private_video_receipt_and_spent_reservation`。

### INV-20 secretを出力しない
APIキー・OAuthトークンをログ・Artifact・トレース属性・コミットに出さない。
YouTube の resumable session URI も同じ扱い（DB の予約行にだけ置く。ADR-0020 §4）。
**機械検査**: 受領 Artifact と Activity 結果に secret / session の欄が無いこと
`tests/contract/test_upload_contracts.py::test_receipt_and_activity_result_have_no_secret_like_fields`。
受領・heartbeat・例外・ログに session URI が出ないこと
`tests/unit/test_upload_activities.py::test_session_expired_after_bytes_without_marker_blocks_and_never_reopens`、
起動時の設定エラーが secret を表示しないこと
`tests/unit/test_upload_worker.py::test_missing_youtube_config_fails_fast_without_printing_secrets`。

## F. 企画（Topic Planner / ADR-0025）

### INV-21 自動生成 Episode には有効な TopicPlan がある
自動生成（Daily）の Episode は `episodes.topic_plan_id` で確定済みの TopicPlan に結び付く。
TopicPlan の確定前に Episode の pipeline（`EpisodePipelineWorkflow`）を始めない。Planner が失敗したら
その日の Episode は作らない。
**機械検査**: `tests/unit/test_pipeline_workflows.py`（Planner 失敗・`topic_plan_id` 無しで pipeline を起動しないこと）
/ `tests/unit/test_topic_planner_workflow.py`

### INV-22 同一 plan_date・strategy・content profile の TopicPlan は1件
`UNIQUE(plan_date, strategy_profile_id, content_profile_id)` を権威とする。Daily の再実行・Activity の再試行は
既存の Plan を再利用し、別の Topic を作らない（決定論的な子 workflow id + find-first）。
**機械検査**: `tests/integration/test_topic_plan_persistence.py` / `tests/unit/test_topic_planner_workflow.py`

### INV-23 LLM の候補出力は TopicCandidate 契約で validation してから保存する
`TopicCandidateBatch` / `TopicCandidate`（`contracts/topic_planning.py`）に通らない出力を DB に入れない。
修復して読まない。形式不正は retryable（ADR-0014）。
**機械検査**: `tests/unit/test_topic_planner_workflow.py` / `tests/unit/test_topic_planning_domain.py`

### INV-24 hard duplicate と cooldown 内の同 subject を選ばない
hard duplicate（exact / semantic）と `same_subject_cooldown_days` 内の同 subject の候補は選ばない。
重複判定は planned / 制作中 / 公開済みの全 Episode（`cancelled` 以外）と全 TopicPlan を対象にする。
**機械検査**: `tests/unit/test_topic_planning_domain.py` / `tests/integration/test_topic_plan_persistence.py`（Content Memory の対象範囲）

## G. 自動運転の維持（Daily Schedule / ADR-0027）

### INV-25 自動で解除してよい Schedule の pause は、ガードの印のある maintenance pause だけ
本番の `avp-daily-episode` は paused=false が望ましい状態（`contracts/schedule_guard.py`）。deploy 等の一時停止は
`AVP-MAINTENANCE/1` の印と期限を持つ maintenance pause として作り、ガード（`infrastructure/temporal/schedule_guard.py`）
だけが解除する。印の無い pause（運用者の緊急停止）は begin / end / reconcile のどれも解除しない。
印が壊れている pause も emergency として扱う。Schedule の定義更新（`--apply`）も pause を外さない。
**機械検査**: `tests/unit/test_schedule_guard.py::test_end_never_unpauses_an_emergency_pause`
/ `tests/unit/test_schedule_guard.py::test_reconcile_never_unpauses_an_emergency_pause_however_old`
/ `tests/unit/test_schedule_guard.py::test_begin_refuses_an_emergency_pause_and_never_touches_it`
/ `tests/unit/test_schedule_guard_domain.py::test_anything_that_is_not_a_valid_marker_is_not_a_maintenance_pause`
/ `tests/unit/test_schedule_registration.py::test_updating_the_daily_schedule_keeps_an_operator_pause`
/ `tests/architecture/test_schedule_guard_boundaries.py::test_only_the_guard_and_the_operator_script_unpause_schedules`。

### INV-26 予定時刻を過ぎて daily が始まっていなければ、翌日を待たずに異常として記録される
Schedule とは別の Schedule（`avp-daily-watchdog`、毎時）が、予定時刻 + 猶予を過ぎてもその日の `daily_episode_slots` も
DailyEpisodeWorkflow も無ければ `DAILY_AUTOMATION_NOT_STARTED` を `operational_anomalies` に記録し、ERROR ログと
`AnomalyNotifier` へ出す。Schedule が pause のままでも記録される（`SCHEDULE_PAUSED_UNEXPECTEDLY`）。
**機械検査**: `tests/unit/test_daily_watchdog.py::test_no_slot_and_no_workflow_records_daily_automation_not_started`
/ `tests/unit/test_daily_watchdog.py::test_a_paused_schedule_is_detected_and_never_unpaused`
/ `tests/unit/test_daily_watchdog.py::test_slot_present_after_grace_is_healthy_with_no_anomaly`
/ `tests/unit/test_daily_watchdog.py::test_the_anomaly_is_recorded_and_notified_once_per_day`。
