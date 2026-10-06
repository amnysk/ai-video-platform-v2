# ログの発行位置（ADR-0040 / log-contract §8）

`contracts.log_contract.EventName` の各値を、どのファイルのどの関数が出すか。発行を足す・動かすときは
同じコミットでこの表を直す（`tests/architecture/test_logging_boundaries.py::test_every_event_name_has_a_documented_emission_point`
が、表と実コードの `EventName.X` の参照を突き合わせる）。

共通の仕組み:

- **Workflow 以外**は `infrastructure.logging.emit.emit()`（record の生成を含めて例外を握る）。
  Workflow は各 workflow module の `_event()`（`workflow.logger` + `extra={"avp": ...}`。`infrastructure`
  を import しない）。
- **commit 後のイベント**（予約台帳・成果物・拒否・認可障害）は repository のメソッドが
  `infrastructure.logging.ledger.defer()` で session に積み、SQLAlchemy の `after_commit` で出す
  （rollback・commit せずに閉じた transaction では出さない。log-contract §9）。
- **文脈**: Activity は `infrastructure.logging.temporal.ActivityLoggingInterceptor`（入力型ごとの
  `ACTIVITY_INPUT_FIELDS`）、有料ジョブは `PaidJobRunner.submit` / `await_output`、API は
  `infrastructure.logging.asgi.RequestLoggingMiddleware`。

| event_name | ファイル:関数 | 備考 |
|---|---|---|
| `log.record` | `infrastructure/logging/formatter.py:JsonFormatter.build`（event_name を持たない記録の既定）、`infrastructure/production/paid_job.py:_dispatch_and_submit`（submit の成否不明）、`workers/production/scene_recovery_activities.py:SceneAlternativeActivities.plan`（代替案の計画・保存の後） | 既存・第三者 logger の記録もこれ |
| `service.started` / `service.stopped` / `service.start_failed` | `infrastructure/runtime/worker_entry.py:run` / `_stopped`、`apps/api/main.py:_lifespan` | 既存の文言（`worker %s starting revision=%s`）は維持 |
| `api.request.completed` | `infrastructure/logging/asgi.py:RequestLoggingMiddleware._completed` | `/healthz` は DEBUG。route template と path の `episode_id` |
| `schedule.slot.acquired` | `workers/pipeline/activities.py:PipelineActivities.claim_daily_slot` | claim の commit の後 |
| `schedule.slot.skipped` | `workers/pipeline/workflows.py:_slot_skipped`（`DailyEpisodeWorkflow.run` から） | paused / limit_reached / no_topic_plan / already_started |
| `stage.started` / `stage.succeeded` | 各工程 workflow の `run` / `_admitted`（`workers/{planning,storyboard,production,render,upload}/workflows.py`）、`workers/pipeline/workflows.py`（`stage=pipeline`） | |
| `stage.failed` / `stage.blocked` | 各工程 workflow の `_settle` / `_settle_failure` / `_stage_settled`（記録した Episode の状態が `blocked` なら blocked）、`workers/pipeline/workflows.py:EpisodePipelineWorkflow._stop` | |
| `stage.skipped` | 各工程 workflow の `run`（入場不可）、`workers/pipeline/workflows.py:EpisodePipelineWorkflow.run`（resume で飛ばした工程） | |
| `activity.started`(DEBUG) / `activity.succeeded` / `activity.failed` | `infrastructure/logging/temporal.py:_LoggingActivityInbound.execute_activity` | cancel は `outcome=cancelled` |
| `provider.call.started` | **未発行（v1）**。語彙だけ予約（DEBUG の想定） | |
| `provider.call.succeeded` / `provider.call.failed` | `infrastructure/logging/provider.py:CallObservation`（`infrastructure/providers/fal_queue.py:FalQueueClient.submit/status/result/download` から）、`infrastructure/providers/fal_storage.py:_log_http_failure` / `_log_transport_failure` / `FalStorageClient.upload`、`infrastructure/youtube/uploader.py:_observe`、`infrastructure/production/paid_job.py:_dispatch_and_submit`（submit の受理 INFO。ref の commit 前） | poll の1回・submit 成功（adapter 側）・YouTube の chunk は DEBUG |
| `provider.job.state_changed` | `infrastructure/production/paid_job.py:PaidJobRunner._await_output` | 試行ごとの最初の観測と状態の変化だけ |
| `provider.auth_incident.recorded` | `infrastructure/db/repositories.py:ProviderAuthIncidentRepository.record` | commit 後 |
| `provider.call.suppressed` | `infrastructure/production/paid_job.py:PaidJobRunner._check_auth_outage_gate` | |
| `reservation.reserved` / `dispatched` / `job_ref_recorded` / `spent` | `infrastructure/db/repositories.py:ProviderReservationRepository.reserve` / `mark_dispatched` / `mark_upload_dispatched` / `record_provider_job_ref` / `record_upload_session` / `mark_spent` / `record_upload_result`・`record_upload_result_once`（`_defer_reservation`） | commit 後。書き手（paid_job・upload・planning・storyboard・scene_recovery）を問わない。同じ video id の再記録（no-op）では出さない（I-20） |
| `reservation.resumed` / `reservation.blocked` | `infrastructure/production/paid_job.py:_resumed` / `_blocked` / `_check_input_fetch_retry` / `_check_rejected_image_gate` | 判断（DB の変更ではない） |
| `artifact.stored` / `artifact.superseded` | `infrastructure/db/repositories.py:ArtifactMetadataRepository._defer_stored` / `_supersede_current` | commit 後 |
| `artifact.reused` | `infrastructure/production/paid_job.py:PaidJobRunner._submit` | 完全性検証（ADR-0033）の後 |
| `artifact.reuse_rejected` | **未発行（v1）**。既存の `ARTIFACT_VERIFICATION_FAILED` の記録（`infrastructure/artifact/verify.py`）は `log.record` | |
| `scene.rejected` | `infrastructure/db/repositories.py:ProviderRejectionRepository.record` | commit 後。`error_code`（観測）と `error_category`（`RejectionCategory`） |
| `scene.input_refetch` | `infrastructure/production/paid_job.py:_check_input_fetch_retry`、`workers/production/workflows.py:_rounds` | |
| `scene.alternative.started` / `scene.alternative.result` | `workers/production/workflows.py:ProductionWorkflow._scene` | `scene_revision`・`storyboard_artifact_id` |
| `episode.resume.requested` / `rejected` / `started` | `apps/api/routers/episodes.py:resume_episode` / `_resume_rejected` | |
| `render.validation.passed` / `render.validation.failed` | `workers/render/activities.py:_log_validation` | |
| `upload.started` / `upload.failed` | `workers/upload/activities.py:UploadActivities.upload_final_video` | |
| `upload.succeeded` | `workers/upload/activities.py:UploadActivities._upload` | 動画1本に1件: この試行が得た video id を**この試行が**台帳に spent として書いた時だけ（`record_upload_result_once` が書いたと返した時）。並行する試行が先に書いていた・予約の時点で spent だった試行は `upload.reused_existing`（レビュー I-20） |
| `upload.reused_existing` | `workers/upload/activities.py:_reused_existing` | YouTube へ送らない（送っていない）。`attributes.found_at`: `ledger`（開始時に spent）/ `reserve`（予約・マーカー照合で spent）/ `record`（並行する試行が先に同じ video id を記録） |
| `upload.skipped` | `workers/pipeline/workflows.py:EpisodePipelineWorkflow.run` | upload gate |
| `research.request.started` / `research.request.finished` | `workers/research/activities.py:ResearchActivities.execute` / `_finished` | `research_request_id` |
| `anomaly.recorded` | `infrastructure/observability/anomaly_notifier.py:LoggingAnomalyNotifier.notify` | `OPERATIONAL_ANOMALY anomaly=` の文言は維持 |

## テストで JSON ログを出す（担当C の隔離検証向け）

既存のテスト（fake provider で 403 / 422 / file_download_error / fallback / reuse / resume を通す
integration test を含む）を、**書き換えずに**本番と同じ JSON ログ・Activity interceptor つきで走らせる。

```bash
AVP_TEST_JSON_LOGS=1 AVP_SERVICE_NAME=pytest-integration AVP_ENVIRONMENT=test \
  .venv/bin/pytest -p tests.support.json_log_plugin \
  tests/integration/test_incident_recovery_e2e.py \
  tests/integration/test_scene_rejection_recovery_e2e.py \
  tests/integration/test_input_fetch_retry_e2e.py \
  tests/integration/test_episode_resume.py \
  > app-logs.jsonl
```

- `tests/support/json_log_plugin.py`（`-p` で明示した時だけ読み込まれる）:
  - `AVP_TEST_JSON_LOGS=1`: 引数なしの `configure_logging()` と同じ設定（env の `AVP_SERVICE_NAME`・
    `AVP_ENVIRONMENT`・`AVP_LOG_FORMAT`・`AVP_LOG_LEVEL`・`AVP_GIT_REVISION` を読む）を、pytest の
    capture が始まる前に複製した fd 1 へ向ける（`-s` 不要。pytest 自身の進捗表示も同じ stdout に混ざるので、
    取り込み側は `{` で始まる行だけを JSON として読む）。
  - テストが自前で組む `temporalio.worker.Worker(...)` に `infrastructure.logging.temporal.worker_interceptors()`
    を足す（`Worker.__init__` を包む。既に `ActivityLoggingInterceptor` があれば足さない）。本番の
    `run_worker.py` も同じ `worker_interceptors()` を使う。
  - `AVP_TEST_BREAK_LOGGING=1`: 全テストでロガーの故障を注入する（INV-38。`break_logging()`）。
  - fixture `broken_logging`: 1つのテストだけで故障を注入する。
- `AVP_LOG_FORMAT=text` で従来の `basicConfig` 形式（`LEVEL:logger:message`）に戻る（安全化はかかる）。
- 自前の plugin から使う場合: `from infrastructure.logging import configure_logging` →
  `configure_logging()`（引数なし。env から読む）、`from tests.support.json_log_plugin import
  install_worker_interceptors, break_logging`。
