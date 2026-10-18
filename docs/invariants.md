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
`apps/ → application/, domain/, contracts/, infrastructure/`、
`workers/ → application/, domain/, contracts/, infrastructure/`、
`application/ → domain/, contracts/`（ADR-0043 で追加。`infrastructure/` は import しない）、
`infrastructure/ → domain/, contracts/`（ADR-0007 で追加。`application/` は import しない）、
`domain/ → contracts/` のみ。
`domain/` は `apps/` `workers/` `infrastructure/` をimportせず、
DB・HTTP・Temporal・ファイルI/O にも触れない。
**機械検査**: `tests/architecture/test_layering.py::test_layer_dependencies_are_one_directional`
/ `::test_domain_has_no_io_dependencies`
/ `::test_application_layer_is_shipped_and_isolated`

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

## H. 共有障害の抑止（ADR-0030）

### INV-27 確認された provider 資格情報障害は同じ provider への新規課金を止める
直近のウィンドウ内で同一 provider に対する認可拒否（401/403）が閾値を超えたら、新しい予約を
作らず `needs_input` で止める。閾値・ウィンドウは `contracts/production_activities.py` の
単一宣言元を持つ。無関係な provider・Episode の処理は継続する（INV-13 と同じ粒度の思想）。
済んだ工程の再開（Artifact 再利用・Submitted の引き継ぎ）はこのゲートの対象外
（provider I/O が要らないため）。
**機械検査**: `tests/unit/test_paid_job.py::test_prepare_auth_failure_records_incident_and_creates_no_reservation`
/ `::test_repeated_auth_incidents_suppress_new_submits_for_same_provider`
/ `::test_auth_outage_gate_is_scoped_to_one_provider`
/ `::test_successful_prepare_resolves_open_incidents`
/ `::test_auth_outage_gate_does_not_block_resuming_already_produced_scenes`

### INV-28 provider 呼び出し失敗の診断情報は secret を含まず構造化して残す
操作種別・HTTP status・provider request id・worker識別子・設定版・発生時刻をログに残す。
Authorization ヘッダ・token・生の応答本文は出さない（INV-20 の具体化）。
**機械検査**: `tests/unit/test_fal_storage.py`（`PROVIDER_AUTH_FAILURE` / `PROVIDER_TRANSIENT_FAILURE`
/ `PROVIDER_REJECTED` の診断フィールドと secret 非漏洩を検査するテスト群）

## I. 自動運転の完走監視（ADR-0031）

### INV-29 watchdog は起動だけでなく進行・完成・投稿を判定する
`blocked` / `needs_work` からの長期停滞、完成期限超過、投稿期限超過（意図した `UPLOADS_PAUSED`
を除く）をそれぞれ検出する。Temporal の workflow 実行が `completed` であることを、Episode の
ドメイン状態と照合せずに成功とみなさない（`PIPELINE_OUTCOME_MISMATCH`）。同日に複数の Episode が
それぞれ問題を起こしても取りこぼさない（`operational_anomalies` の episode 単位インデックス）。
**機械検査**: `tests/unit/test_daily_watchdog.py` / `tests/contract/test_operational_anomalies_episode_scope.py`

## J. 途中再開（ADR-0032）

### INV-30 Episodeの統一再開は日次枠を再消費せず、同一Episodeの二重実行を作らない
再開は `daily_episode_slots` を消費しない（`claim_daily_slot` を呼ばない）。同じ Episode に対する
二重の再開要求は、決定論的な workflow id（`pipeline_workflow_id`）への Temporal の
`WorkflowAlreadyStartedError` が構造的に防ぐ（実行と課金が重複しない）。
read-onlyのdry-run（`GET /episodes/{id}/resume/plan`）はProvider呼び出し・予約作成・workflow起動を
一切行わない（`WorkflowStarter` を依存に注入しない構造で保証する）。
**機械検査**: `tests/unit/test_resume_plan.py` / `tests/unit/test_resume_api.py`
/ `tests/unit/test_pipeline_workflows.py` / `tests/integration/test_episode_resume.py`

## K. Artifact再利用の完全性（ADR-0033）

### INV-31 Artifactの再利用は実体を検証してから行う
DB行の存在だけで再利用しない。MinIO実体の存在・size・sha256、schema検証可能な型は読み戻し、
生成設定版（image/video の固定 provider profile id、render の `RENDER_PROFILES`）の互換性を
確認する。欠落・破損・版不一致は「現行が無い」として扱い、新しいラウンド（regenerate）へ進む。
検証で破損を検出しても、既存の MinIO object・`artifact_metadata` 行・`provider_reservations` 行を
自動で削除・変更しない。通常パイプライン（production/render/upload の各Activity）は同じ唯一の
関数（`infrastructure.artifact.verify.find_and_verify_current`）を経由し、判定を二重化しない。
**機械検査**: `tests/unit/test_artifact_verification.py` / `tests/unit/test_artifact_verify_io.py`
/ `tests/unit/test_paid_job.py::test_corrupt_artifact_does_not_bypass_the_unreconciled_reservation_block`

### INV-32 provider に拒否された入力は自動で再送しない ── 同じ入力も、同じ入力画像も
provider が内容を拒否した入力（同じ `input_hash`、ADR-0034）は新しいラウンドを作らない。加えて、
拒否の対象が入力画像（`provider_rejections.rejected_input = 'image'`）なら、その画像の sha256 を
同じ provider へ、テキストを変えても再送しない（予約 INSERT の前に止め、予約も課金も作らない）。
拒否は adapter が応答から組み立てた構造（対象・理由・種別）で `provider_rejections` に1件ずつ残し、
`error_summary` の文字列から推測しない（ADR-0035）。
**機械検査**: `tests/unit/test_paid_job.py::test_rejected_image_is_not_resubmitted_with_different_text`
/ `::test_content_rejection_is_recorded_structured_with_the_input_image`
/ `::test_a_different_image_is_not_blocked` / `::test_a_prompt_rejection_does_not_block_the_image`
/ `::test_provider_rejected_input_blocks_the_next_round`（ADR-0034）
/ `tests/unit/test_fal_queue.py::test_content_policy_rejection_is_structured_from_the_body`

### INV-33 1シーンの映像の差し替えとレシピ版の変更は、そのシーンと依存成果物以外を再生成・再課金しない
画像・動画の `input_hash`（方式2、ADR-0035 (4)）はそのシーンの**実効内容**（storyboard のシーン +
現行の代替映像案 `scene_visual_override`）の指紋を材料にし、別シーンの内容を含まない。代替映像案は
storyboard の世代を変えないので、差し替えていないシーンの hash は変わらない。prompt 組み立て規則の版
（`IMAGE_PROMPT_BUILDER_VERSION` / `VIDEO_PROMPT_BUILDER_VERSION`）だけが変わった成功済みの成果物は
`artifact_metadata.content_fingerprint` で再利用する（生成器・モデルの違いは再利用しない）。
旧方式（`content_fingerprint` が NULL の本番行・予約）は旧方式の hash を版 1〜現在で再計算して照合し、
hash の方式が変わっただけで進行中の課金ジョブへ二重 submit したり、provider に拒否された入力を
再送したりしない。代替映像案のあるシーンでは旧方式の成果物（拒否された元の画像）を再利用しない。
**機械検査**: `tests/unit/test_scene_identity_v2.py` / `tests/unit/test_scene_identity_reuse.py`

### INV-34 内容拒否からの自動復旧は回数と費用に上限があり、超えたら人間の判断を待つ
拒否されたシーンの代替映像案は、1シーンあたり `MAX_SCENE_ALTERNATIVES_PER_SCENE`、1 Episode あたり
`MAX_SCENE_ALTERNATIVES_PER_EPISODE` 回まで、かつ復旧の追加費用（拒否された spent 予約と、代替案の後に
作り直した fal 予約の `estimated_cost_usd` の合計 + 次の作り直しの見積り）が
`MAX_RECOVERY_COST_USD_PER_EPISODE` 以下の間だけ自動で作る（定義元は
`contracts/production_activities.py` の1箇所）。回数・費用は DB から数え、resume でリセットしない。
上限到達・planner の不成立（理由つき）・規則違反（人物を主題にする・既に試した文面・根拠なし）は
`needs_input` で止まる。workflow は1回の実行で planner を1シーンの上限回数より多く呼ばない（ADR-0035）。
**機械検査**: `tests/unit/test_scene_alternative_activity.py::test_scene_limit_stops_automation`
/ `::test_cost_cap_stops_automation_before_calling_the_planner`
/ `::test_infeasible_plan_stops_with_the_planners_reason`
/ `::test_a_person_subject_after_a_likeness_rejection_is_not_saved`
/ `::test_blocked_again_on_the_same_plan_does_not_loop`
/ `tests/unit/test_scene_alternative_rules.py::test_limits_stop_automation`
/ `tests/unit/test_production_scene_recovery_workflow.py::test_workflow_never_asks_the_planner_more_than_the_scene_limit`
/ `tests/unit/test_scene_alternative_activity.py::test_limits_come_from_the_injected_settings`
/ `::test_non_content_policy_failures_never_reach_the_planner`（代替案の対象は `content_policy` だけ、ADR-0035 (8)）
/ `tests/integration/test_input_fetch_retry_e2e.py::test_alternative_limit_from_settings_stops_further_generation`

### INV-35 provider が入力を取得できなかったら、新しい入力 URL で最大1回だけ自動再試行し、2回目で止まる
provider がこちらの入力（URL のファイル）を取得できなかった失敗（分類 `input_unreachable`。fal の
`file_download_error`）は内容の拒否ではない。そのシーンについて、入力を上げ直した新しい URL で
**最大 `INPUT_FETCH_RETRIES_PER_SCENE`（= 1）回だけ**台帳の新ラウンドとして自動で再試行する。回数は
`provider_rejections` の `input_unreachable` 件数から数え、resume でリセットしない。上限を超えた新ラウンドは
予約の前に止める（予約も課金も作らない）。成功済みの他シーンは触らない。代替映像案は計画せず、内容拒否の
復旧回数・費用（INV-34）にも数えない。画像の再送禁止（INV-32）の対象にしない（ADR-0035 (8)）。
**機械検査**: `tests/unit/test_paid_job.py::test_unreachable_input_is_recorded_but_not_as_a_rejected_input`
/ `::test_second_fetch_failure_stops_before_reserving_a_third_round`
/ `::test_image_gate_ignores_unreachable_and_unknown_but_blocks_validation`
/ `tests/integration/test_production_workflow.py::test_input_fetch_failure_retries_once_with_a_new_round_and_succeeds`
/ `::test_second_input_fetch_failure_in_the_run_stops_the_episode`
/ `::test_input_fetch_retry_is_granted_even_with_a_single_round_budget`
/ `::test_ledger_exhaustion_on_resume_stops_without_another_submit`
/ `tests/integration/test_input_fetch_retry_e2e.py::test_transient_input_fetch_failure_is_retried_once_with_a_new_url_through_upload`
/ `::test_second_input_fetch_failure_stops_and_resume_does_not_submit_again`
/ `tests/contract/test_migration_frozen_vocabulary.py::test_0014_legacy_file_download_error_is_backfilled_as_unreachable_not_rejected`

## L. Research（ADR-0037）

### INV-36 Research 依頼の外部呼び出しは、依頼ごとの上限を超えない
1 つの Research 依頼が行う外部呼び出し（検索・本文取得・評価）は、依頼を受けた時点で凍結した
`limits` から決まる種別ごとの上限（`contracts.research.call_ceiling`）を超えない。呼び出しは
`research_calls` に呼ぶ前に予約し、`reserved` / `spent` / `abandoned` のどの行も枠を数え、
`call_seq` は再利用しない。アプリの採番に誤りがあっても、DB の `UNIQUE(request_id, provider_call, call_seq)`
と `call_seq >= 1` が上限より多い行を拒否する。dispatch した呼び出しは `abandoned` にできない。
**機械検査**: `tests/unit/test_research_call_ledger.py::test_the_ceiling_stops_the_next_reservation_before_insert`
/ `::test_abandoned_and_spent_calls_still_count_and_seq_is_never_reused`
/ `::test_re_reserving_the_same_key_returns_the_same_call_and_uses_no_budget`
/ `::test_the_database_rejects_a_duplicate_call_seq`
/ `::test_a_racing_writer_does_not_push_the_count_past_the_ceiling`
/ `tests/contract/test_migration_0015_research.py::test_the_database_rejects_a_second_row_with_the_same_call_seq`
/ `tests/contract/test_migration_0015_research.py::test_the_database_rejects_invalid_ledger_rows`
/ `tests/unit/test_research_executor.py::test_reaching_the_ledger_ceiling_stops_the_calls_and_finishes_partial`
/ `tests/unit/test_research_executor.py::test_an_ambiguous_call_is_not_resent_and_blocks_the_request`
（本物の並行トランザクションでの検査は未移植。PostgreSQL の integration テストは Worker の段で足す）

### INV-37 Research は本番の表と課金コードに触れず、Episode 本番工程は Research を待たない
Research は `jobs` / `artifact_metadata` / `provider_reservations` / `provider_rejections` に書かず、
本番のリポジトリ（`infrastructure/db/repositories.py`）と課金コード（`infrastructure/production/`、
`PaidJobRunner`）を import しない。research の表は research の表だけを FK で指し、本番の表は research の表を
指さない。本番の工程（production / render / upload / storyboard / pipeline / 課金）は Research を
import しない。Research の結果が `completed` でなければ、呼び出し側は「調査なし」として調査前の挙動で続ける。
「待たない」は既定（opt-in OFF）の意味で、唯一の例外は `SCRIPT_EVIDENCE_ENABLED` の台本工程である。そこでは
`script_ready` の前に上限つき（`SCRIPT_EVIDENCE_START_TO_CLOSE` × `SCRIPT_EVIDENCE_RETRY_POLICY` の試行回数）で
照合を待つが、どの結果・失敗・timeout でも Episode は止まらない（助言。ADR-0038 §B6）。
**機械検査**: `tests/architecture/test_research_isolation.py`
/ `tests/contract/test_migration_0015_research.py::test_upgrade_adds_only_research_tables_and_leaves_production_tables_alone`
/ `tests/contract/test_migration_0015_research.py::test_research_tables_only_reference_research_tables`
/ `tests/architecture/test_research_isolation.py::test_research_execution_does_not_wire_a_real_provider`
/ `tests/architecture/test_daily_does_not_wait_for_research.py`（日次・pipeline・企画・台本は Research の
workflow・queue・起動・実行器を名指さない。ADR-0037 §8.5）
/ `tests/unit/test_research_opt_in_off_path.py`（B6: 企画・台本への opt-in 接続は既定 OFF。OFF の worker は
Research のコードを読み込まず、登録する workflow・Activity、Topic の prompt テンプレートと版、台本の同一性は
f209e7c と同じ）
/ `tests/unit/test_planner_trend_opt_in.py::test_off_prompt_and_version_are_byte_identical_to_f209e7c`
（OFF と「Trend 無し」の Planner の prompt・版は f209e7c の golden とバイト単位で同じ）
/ `tests/unit/test_script_evidence_workflow_opt_in.py::test_the_f209e7c_off_history_replays_on_both_workers`
/ `tests/unit/test_script_evidence_workflow_opt_in.py::test_a_new_off_run_has_the_same_history_shape_as_f209e7c`
（OFF の ScriptWorkflow の履歴は接続前と同じ。ON の照合は結果によらず `script_ready` へ進む助言:
`::test_an_on_run_checks_evidence_once_and_always_reaches_script_ready`）
/ `tests/architecture/test_research_opt_in_boundary.py`（接続は `workers/planning` の 3 モジュールに閉じる。
Workflow と本番工程はそれを import しない）
/ `tests/architecture/test_research_isolation.py::test_the_planning_links_do_not_touch_production_billing_or_artifacts`
（B6 で追加。ADR-0038 §B6 / ADR-0039 §B6）

## M. 構造化ログ（ADR-0040）

### INV-38 ログの障害は業務を止めず、業務状態・課金・例外の型を変えず、自動判断の根拠にならない
ログの発行・整形・収集・検索基盤の障害は業務処理を失敗・停止させず、業務状態・課金・例外の型を変えない。
発行ヘルパー（`infrastructure.logging.emit`）は record の生成を含めて例外を握り、整形器は失敗を固定形で
出し直し、Activity interceptor・API middleware は記録してから**同じ例外オブジェクト**を再送出する。
アプリは stdout にしか書かず、OpenSearch を import・接続しない。ログ検索の結果を課金判定・再実行・
再開・投稿の自動判断に使わない（人手の照合の手がかりにはしてよい。確定は DB・Temporal・provider 側）。
予約台帳・成果物のイベントは commit が成功した後にだけ出る。
**機械検査**: `tests/unit/test_log_fault_injection.py::test_ledger_suites_pass_unchanged_with_broken_logging`
（既存の台帳・有料 submit/await・fal adapter のテストを書き換えずにロガーを壊して全部通す）
/ `tests/unit/test_log_fault_injection.py::test_the_fault_injection_really_breaks_emission`
/ `tests/unit/test_log_emit.py::test_emit_never_raises_even_when_the_logger_is_broken`
/ `tests/unit/test_log_activity_interceptor.py::test_a_broken_logger_does_not_change_the_activity_outcome`
/ `tests/unit/test_log_activity_interceptor.py::test_failure_is_recorded_and_the_same_object_is_reraised`
/ `tests/unit/test_log_api.py::test_an_exception_is_logged_and_reraised_unchanged`
/ `tests/unit/test_log_formatter.py::test_a_broken_record_falls_back_to_the_fixed_minimal_form`
/ `tests/unit/test_log_ledger.py::test_rolled_back_or_uncommitted_changes_are_not_logged`
/ `tests/architecture/test_logging_boundaries.py::test_nothing_imports_opensearch`
/ `tests/architecture/test_logging_boundaries.py::test_log_extra_uses_only_the_avp_key`
（自動判断に使わないことは機械で検査できない。運用文書と ADR-0040 で禁じる）

### INV-39 秘密・provider 応答全文・prompt 全文・メディアのバイト列は stdout に出る前に除去される
Python logging を経由する全ての記録（第三者 logger・`warnings`・未捕捉例外・Temporal Core の転送を含む）は、
stdout に出る前に同じ整形器で安全化される: 秘密を示すキーの値、Bearer/Basic/Key・fal key・JWT・private key・
Google token・`sk-`・DSN の userinfo・SQLAlchemy の `[parameters: …]`・長い base64 を置換し、URL の
query・userinfo を落として許可 host（adapter の定数から導く）以外の path を縮約し、provider 応答は許可した
項目だけを入れる。置換してから切り詰める。迂回経路（logging 設定前の起動失敗・native クラッシュの stderr・
migrate の alembic・CLI provider の stderr を含む例外文）は log-contract §7.6 に残るリスクとして記録する。
**機械検査**: `tests/unit/test_log_redaction.py::test_value_patterns_are_replaced`
/ `tests/unit/test_log_redaction.py::test_message_attributes_and_exception_text_are_cleaned`
/ `tests/unit/test_log_redaction.py::test_third_party_logger_goes_through_the_same_formatter`
/ `tests/unit/test_log_redaction.py::test_uncaught_exception_goes_through_the_formatter`
/ `tests/unit/test_log_redaction.py::test_warnings_are_captured`
/ `tests/unit/test_log_redaction.py::test_temporal_core_logs_are_forwarded_not_written_to_stderr`
/ `tests/unit/test_log_redaction.py::test_allowed_hosts_are_derived_from_the_adapter_constants`
/ `tests/unit/test_log_redaction.py::test_other_hosts_are_shrunk_to_a_hash`
/ `tests/unit/test_log_provider_calls.py::test_422_content_policy_uses_the_provider_error_type`
/ `tests/unit/test_log_activity_events.py::test_upload_started_succeeded_then_reused_existing`
/ `tests/unit/test_log_api.py::test_route_template_episode_id_and_request_id`

### INV-40 Workflow のログ発行は決定性を崩さず、replay で業務イベントを重複発行しない
Workflow のコードは `infrastructure` を import せず、`workflow.logger` と `extra={"avp": {...}}` だけで発行し、
時刻・乱数・UUID・I/O を足さない（コマンドを足さない）。replay 中の発行は SDK が抑止する。`event_id` は
sandbox の外の整形器が `uuid5(workflow_id:run_id:history_length:seq:event_name)` で導く。workflow task が
発行の後に失敗・timeout した場合の再発行（別 ID）は起こり得る（task の試行単位で at-least-once）。
**機械検査**: `tests/unit/test_log_workflow_replay.py::test_workflow_events_are_emitted_once_and_replay_emits_nothing`
/ `tests/architecture/test_logging_boundaries.py::test_workflow_modules_do_not_import_infrastructure`
/ `tests/unit/test_log_formatter.py::test_workflow_event_id_is_deterministic_and_every_input_matters`
/ `tests/unit/test_log_formatter.py::test_workflow_event_id_does_not_collide_across_a_grid`
/ `tests/unit/test_log_old_history_replay.py::test_old_history_replays_deterministically_and_emits_nothing`
（ログ導入前 01eb0ee のコードで採った Production（代替映像案の成功・上限を含む）・Render・Upload・Storyboard・
Script（成功・再ラウンド後の成功・既存の再利用・blocked 3経路）・EpisodePipeline・Daily の履歴を、今のコードの
Replayer で replay。非決定にならず1件も発行しない）
/ `tests/unit/test_log_old_history_replay.py::test_old_script_history_replays_on_the_evidence_worker_too`
/ `tests/unit/test_log_old_history_replay.py::test_the_old_histories_cover_every_stage_workflow`
/ `tests/architecture/test_logging_boundaries.py::test_workflow_event_names_and_stages_are_contract_vocabulary`
/ `tests/unit/test_pipeline_workflows.py`（既存の履歴 fixture の replay がログ発行を足した後も通る）

## N. 判断記録と介入（ADR-0041）

### INV-41 ユーザー修正は新 revision として保存し、旧版・コメントを消さない
ChangeSet の適用（`infrastructure.intervention.changes.apply_change`）は、新しい成果物を不変のオブジェクトとして
保存し `artifact_metadata` に新しい世代を記録する（旧行は superseded として残り、旧オブジェクトも残る）。
`revision_links`（`parent`、構成・尺が同じ台本修正は storyboard への `rebound_to`）、新しい採用 manifest、
ChangeSet の作者を actor とする判断記録（`override_diff`）、古い manifest の approval ごとの `review.invalidated` を
同じ commit に入れる。影響範囲の下流（音声・production_manifest・final_video／そのシーンの画像・動画）は現行から
降ろすだけで行は消さない。台本の文面の修正は画像・動画（有料）の現行・入力指紋・storyboard を変えない。
送信済みの有料 job があるシーンの差し替えは受け付けない（旧版の結果が新しい採用 manifest に混ざらない）。
**機械検査**: `tests/unit/test_intervention_changes.py::test_script_edit_creates_a_new_revision_and_keeps_the_old_one_and_comments`
/ `tests/unit/test_intervention_changes.py::test_script_edit_does_not_touch_image_or_video_identity`
/ `tests/unit/test_intervention_changes.py::test_scene_override_reuses_the_override_artifact_and_regenerates_only_that_scene`
/ `tests/unit/test_intervention_changes.py::test_an_old_version_result_cannot_become_the_adopted_member_after_a_change`
/ `tests/unit/test_intervention_changes.py::test_ai_may_apply_only_zero_cost_changes_that_respect_user_constraints`
/ `tests/unit/test_intervention_changes.py::test_a_paid_change_without_a_rate_is_unverifiable_and_with_a_rate_is_checked`
/ `tests/unit/test_intervention_changes.py::test_a_paid_change_without_a_ledger_estimate_is_unverifiable`

### INV-42 同じ command_id の再送・復旧は1つの論理変更にしかならない
介入 command は `intervention_commands` の行（`id` = クライアントの command_id）として受け付け、同じ id・同じ
payload（`payload_sha256`）の再送は既存の行を返し、違う payload は `DuplicateCommandError`（API 409）。
適用（`infrastructure.intervention.commands.apply_command`）は Episode の制御行を lock して直列化し、
command が既に終端（applied / conflict / rejected / failed）なら保存済みの result を**そのまま返す**
（状態・revision・manifest・イベントを増やさない）。子 pipeline の start の結果は result に追記され、
記録後の再配送は pipeline を二度 start しない。
**機械検査**: `tests/unit/test_intervention_commands.py::test_same_id_and_payload_returns_the_existing_command`
/ `tests/unit/test_intervention_commands.py::test_same_id_with_another_payload_is_refused`
/ `tests/unit/test_intervention_commands.py::test_applying_the_same_command_twice_is_one_logical_change`
/ `tests/unit/test_intervention_commands.py::test_comment_is_applied_at_receipt_and_reapply_returns_the_stored_result`
/ `tests/unit/test_intervention_commands.py::test_resume_runs_again_from_the_next_stage`

### INV-43 期待版の不一致は CONFLICT で返し、上書きしない
`expected_control_revision`（Episode の制御版）が現在値と違う command は `conflict`
（`stale_control_revision`）で終わり、制御状態・採用を変えない。checkpoint の解放は承認する manifest が
現在の採用 manifest と一致しなければ `conflict`（古い manifest への承認を流用しない）。
ChangeSet の基準 revision・`expected_target_revision` が現行でなければ `conflict`（ChangeSet は `conflicted`）。
**機械検査**: `tests/unit/test_intervention_commands.py::test_a_stale_expected_control_revision_is_a_conflict_and_changes_nothing`
/ `tests/unit/test_intervention_commands.py::test_release_checkpoint_binds_the_approval_to_the_active_manifest`
/ `tests/unit/test_intervention_changes.py::test_a_stale_base_revision_is_a_conflict_and_marks_the_change_set`
/ `tests/unit/test_intervention_changes.py::test_an_expected_target_revision_mismatch_is_a_conflict`
/ `tests/unit/test_intervention_changes.py::test_proposals_against_a_stale_base_are_conflicts_and_do_not_create_change_sets`

### INV-44 hold の受付後に新しい dispatch は始まらず、HELD は送信済み・成否不明を列挙する
`request_hold` は受付の transaction で `episode_controls.control_state = hold_requested` にし、dispatch gate
（`mark_dispatched` / `mark_upload_dispatched` の同じ transaction で制御行を `FOR SHARE` で読む）は `running` 以外の
Episode の新しい有料 dispatch・投稿を `EpisodeHeldError` で拒否する。拒否された予約は `reserved` かつ未 dispatch の
まま残り、resume 後に同じ予約（同じ冪等キー）で再利用される。適用で `held` にしたときの result は、送信済み
（`in_flight`）・成否不明（`unknown_outcome`）・未送信（`undispatched`）の予約を列挙する（provider の job 参照は載せない）。
停止は「以後の追加課金ゼロ」を意味しない（送信済みは完了・課金されうる）。
**機械検査**: `tests/unit/test_intervention_gate.py::test_a_non_running_episode_refuses_dispatch_and_leaves_the_reservation_unsent`
/ `tests/unit/test_intervention_gate.py::test_upload_dispatch_is_refused_while_held_but_an_in_flight_session_is_not_reopened`
/ `tests/unit/test_intervention_gate.py::test_a_reservation_refused_by_the_gate_is_reused_after_resume`
/ `tests/unit/test_intervention_commands.py::test_hold_is_requested_in_the_receiving_transaction_and_not_reported_as_done`
/ `tests/unit/test_intervention_commands.py::test_hold_becomes_held_and_lists_in_flight_unknown_and_undispatched`
（PostgreSQL の行 lock での hold と dispatch の競合の integration テストは未作成。単体は SQLite）

### INV-45 manifest が変われば、それに結び付いた approval は stale になり流用されない
approval（`annotations.kind=approval`）は承認した採用 manifest（`annotations.manifest_id`）に結び付く。
採用 manifest が別の manifest に替わった時点で（台本・素材・完成品のどれかが新しい revision になれば
新しい manifest になる）、旧 manifest の approval は現行の合格として扱わない。判定は
`infrastructure.intervention.read_model.approval_is_current(annotation, control)` の1つだけで、
`annotation.manifest_id == control.active_manifest_id` のときだけ真（approval 以外・manifest 未結び付け・
採用 manifest 無しは偽）。読み取りモデルは旧 manifest の approval を `stale_approvals` に列挙し、
`approval_current=false` で示す。approval の行は消さない（追記型 / INV-41 の趣旨）。
**機械検査**: `tests/unit/test_intervention_read_model.py::test_approval_is_current_only_for_the_active_manifest`
/ `tests/unit/test_intervention_read_model.py::test_an_approval_of_the_old_manifest_is_stale_after_the_manifest_changes`
（checkpoint の解放・投稿の直前にこの述語を通すのは担当B の適用・gate 側。その検査はそちらのテストが持つ）

### INV-46 新方式（mode != legacy）では、必須記録の無い有料生成・投稿を開始しない
`episode_controls.mode` が `legacy` 以外の Episode では、有料 dispatch・投稿の開始許可
（`mark_dispatched` / `mark_upload_dispatched` の同じ transaction の dispatch gate
`infrastructure.intervention.gate.check_dispatch_allowed`）が、現行の script・storyboard の各 revision に
生成した `agent_runs`（`output_revision_id`）か人の変更を表す `decision_records`（`result_revision_id`）が
結び付いていることを検査し、欠けていれば `MissingDecisionRecordError` で拒否する（予約は未 dispatch のまま）。
`episode_controls` 行の無い Episode・`legacy` の Episode は従来どおり（gate は何も書かない）。
**機械検査**: `tests/unit/test_intervention_gate.py::test_new_mode_requires_a_record_for_the_current_script_and_storyboard`
/ `tests/unit/test_intervention_gate.py::test_legacy_mode_with_a_row_does_not_require_records`
/ `tests/unit/test_intervention_gate.py::test_an_episode_without_a_control_row_dispatches_and_nothing_is_written`

### INV-47 介入 API は予算上限・Schedule・公開設定を変更できず、AI の principal は人専用 scope を得られない
介入 API（`/v1/intervention`、`apps/api/routers/intervention.py`）の書き込みは command の受付（`POST .../commands` と
変更案ファイルの取り込み `POST .../change-proposals`）だけで、他の route は GET。command 型の語彙
（`contracts.intervention.CommandType`）に予算上限・Schedule・公開設定を変える型は無く、未知の型は
`422 unsupported_command`、既存の型の payload に余分な項目（予算・cron・公開範囲など）を混ぜると `422 invalid_payload`
（payload model は `extra="forbid"`）。principal は設定 `INTERVENTION_PRINCIPALS`（token の sha256 → actor・scope）で
決まり、AI（`ai_assistant` / `agent`）に `HUMAN_ONLY_SCOPES`（`set_mode`）を与える設定は読み込まない
（介入 API は `503 intervention_misconfigured`）。設定が無ければ介入 API は全て `503 intervention_disabled`（既定で閉じる）。
command には実際に使った scope（`granted_scope`）と主体を記録する。
**機械検査**: `tests/contract/test_migration_0016_intervention.py::test_no_command_type_changes_budget_schedule_or_visibility`
/ `tests/unit/test_intervention_api.py::test_budget_schedule_and_visibility_commands_are_422_unsupported`
/ `tests/unit/test_intervention_api.py::test_budget_schedule_visibility_fields_cannot_ride_on_other_commands`
/ `tests/unit/test_intervention_api.py::test_the_router_exposes_no_budget_schedule_or_visibility_surface`
/ `tests/unit/test_intervention_api.py::test_an_ai_principal_cannot_set_mode`
/ `tests/unit/test_intervention_api.py::test_every_intervention_route_is_503_when_unconfigured`
/ `tests/unit/test_intervention_api.py::test_a_misconfigured_principal_file_disables_the_api_without_leaking`
/ `tests/unit/test_intervention_api.py::test_a_new_command_is_202_received_and_started_after_commit`（`granted_scope`・主体の記録。保存は担当 B の `receive_command` の fake を通した store）
/ `tests/unit/test_intervention_auth.py::test_ai_principals_cannot_be_granted_human_only_scopes`
/ `tests/unit/test_intervention_auth.py::test_system_actor_cannot_be_an_api_principal`
/ `tests/unit/test_intervention_auth.py::test_settings_default_is_unconfigured`
（Settings の予算・Schedule の値を API から変える経路が無いことは route の表面と command 語彙で検査する。
担当 B の `receive_command` が `granted_scope` を `REQUIRED_SCOPE` から記録することは B のテストと統合後の検査に依る）

### INV-48 対話 AI アシスタントの adapter は介入 HTTP API と contracts だけを使う
`apps/assistant/**`（対話 AI 向け tool の実行器。ADR-0042）が import してよいのは標準ライブラリ・`httpx`・
`contracts.*`・自分自身だけ。`infrastructure`・`workers`・`domain`・`temporalio`・`sqlalchemy`・`psycopg`・
`apps.api` 等には触れない。DB や Temporal を直接読み書きすると、介入 API が持つ認可（scope）・期待版・
AI による有料適用の拒否・command の記録（INV-47）を迂回する経路になる。tool の名前・入力 schema・効果・必要 scope の
唯一の宣言元は `contracts/assistant_tools.py`（command を送る tool の scope は `REQUIRED_SCOPE` から導く）。
予算上限・Schedule・公開設定を変える tool は定義しない。
**機械検査**: `tests/architecture/test_assistant_boundary.py::test_the_assistant_package_imports_only_http_and_contracts`
/ `tests/architecture/test_assistant_boundary.py::test_no_forbidden_module_is_named_even_in_a_lazy_import`
/ `tests/architecture/test_assistant_boundary.py::test_the_cli_entry_point_only_wraps_the_adapter`
/ `tests/architecture/test_assistant_boundary.py::test_the_check_itself_rejects_internal_imports`
/ `tests/contract/test_assistant_tools_contract.py::test_command_tools_derive_their_scope_from_the_single_source`
/ `tests/contract/test_assistant_tools_contract.py::test_no_tool_changes_budget_schedule_or_visibility`
/ `tests/contract/test_assistant_tools_contract.py::test_proposal_schemas_do_not_invent_payload_fields`
/ `tests/contract/test_assistant_tools_contract.py::test_a_schema_valid_proposal_is_accepted_by_the_real_payload_model`
/ `tests/architecture/test_assistant_boundary.py::test_each_detection_fires_on_a_fake_package`
/ `tests/architecture/test_assistant_boundary.py::test_from_apps_import_assistant_is_not_a_false_positive`
/ `tests/unit/test_assistant_adapter.py::test_every_tool_in_the_catalogue_can_be_dispatched`

### INV-49 同じ tool 呼び出しの再試行は1つの論理変更にしかならず、費用は確認したものだけを適用する
command を送る tool の `command_id` は `uuid5(ASSISTANT_NAMESPACE, "{session_id}:{tool_call_id}")`
（`contracts.assistant_tools.tool_command_id`）で決まる。同じ tool 呼び出しの再試行は同じ command_id になり、
INV-42 により API は保存済みの command を返す（二重の提案・適用・課金にならない）。`apply_change` は
`change_set_id` と提示した見積り `acknowledged_estimated_cost_usd`（未見積りは明示的な `null`）を必須にし、
送信直前に変更案を読み直して一致しなければ `cost_changed` を返して何も送らない（`null` と `0` を同一視しない）。
サーバが権限不足・AI の有料適用で拒否したら `needs_human_approval`（影響・見積り・人が実行する
`scripts/intervene.py apply …`）を返し、再試行や別の資格情報での迂回をしない。全ての要求に
`X-Correlation-ID: assistant:{session_id}:{tool_call_id}` と `X-Assistant-Tool` を付け、API は
`intervention.api.request` を記録する（token・Authorization・本文は出さない。INV-39）。command 行に保存する
相関 ID は 64 文字を超えるとき `assistant:` + 32 桁の hex（完全な文字列の sha256 の先頭）の短縮形で、ヘッダ・ログには
完全な値が残る。同じ `tool_call_id` が別の command（種類・ChangeSet・Episode が違う）に使われていたら
`command_id_reused` を返し、保存済みの別 command を結果として返さない。アシスタントの token は AI の principal に限る。
**機械検査**: `tests/contract/test_assistant_tools_contract.py::test_command_id_is_deterministic_per_tool_call`
/ `tests/unit/test_assistant_e2e.py::test_a_retried_tool_call_is_one_logical_change`（本物の FastAPI アプリ・store）
/ `tests/unit/test_assistant_e2e.py::test_a_new_tool_call_is_a_new_command_and_a_reused_id_with_other_input_is_refused`
/ `tests/unit/test_assistant_e2e.py::test_apply_with_a_stale_cost_sends_no_command_to_the_real_api`
/ `tests/unit/test_assistant_adapter.py::test_the_same_tool_call_sends_the_same_command_id_and_a_different_one_differs`
/ `tests/unit/test_assistant_adapter.py::test_a_retried_apply_returns_the_stored_command_without_rechecking_cost`
/ `tests/unit/test_assistant_adapter.py::test_a_tool_call_id_used_for_another_command_is_command_id_reused_not_applied`
/ `tests/unit/test_assistant_adapter.py::test_a_stored_apply_for_another_change_set_or_episode_is_command_id_reused`
/ `tests/unit/test_assistant_adapter.py::test_human_command_is_built_only_from_strict_uuids_and_quoted`
/ `tests/unit/test_assistant_adapter.py::test_a_changed_estimate_sends_nothing_and_returns_cost_changed`
/ `tests/unit/test_assistant_adapter.py::test_null_and_zero_are_never_confused`
/ `tests/unit/test_assistant_adapter.py::test_a_forbidden_scope_returns_needs_human_approval_and_does_not_retry`
/ `tests/unit/test_assistant_adapter.py::test_an_ai_paid_apply_rejected_by_the_server_becomes_needs_human_approval`
/ `tests/unit/test_assistant_adapter.py::test_every_request_carries_the_correlation_and_tool_headers`
/ `tests/unit/test_assistant_adapter.py::test_the_token_never_appears_in_results_errors_or_repr`
/ `tests/unit/test_assistant_request_log.py::test_neither_token_nor_authorization_nor_body_is_logged`
/ `tests/unit/test_assistant_e2e.py::test_the_server_logs_the_assistant_correlation_and_never_the_token`

## O. 自律改善型制作（ADR-0043）

### INV-50 単一変更経路: StoryboardPatchProposal から propose_change を作るのは `ProposeStoryboardChange` だけ
絵コンテの変更提案（`contracts.application.StoryboardPatchProposal`）を `propose_change` command に変えるのは
`application/use_cases/propose_storyboard_change.py` の1か所だけ。HTTP（`POST /v1/intervention/episodes/{id}/storyboard/proposals`）・
アシスタント tool（`propose_storyboard_patch`）・内部 Agent（`SessionCommandPort`）・CLI は提案を use case へ通すだけで、
`SceneOverrideVisual` への変換や edit ごとの command_id（`storyboard_edit_command_id`）の導出を持たない。
入口が違っても結果型（`StoryboardProposalResult`）と拒否 code（`OperationErrorCode`）は同じ。P0 が受け付けるのは
`visual_description` だけで、他の項目は `unsupported_change`。同じ要求の再送は既存 command（INV-42）、
同じ `request_command_id` で別内容は `command_id_reused`。
要求全体の指紋（`storyboard_request_sha256`: 全 edit・件数・順序・基準）を全 command の payload（`request_sha256`）に載せるので、
**edit 数だけが違う再送**（部分集合・余分な edit）も先頭 command で `command_id_reused` になり、1 transaction で全て拒否される
（部分適用なし）。use case は適用しない。新機能は `JPMAS2_APPLICATION_API`
（既定 false）が false なら `404 feature_disabled`。
**機械検査**: `tests/architecture/test_single_change_path.py::test_only_pass_through_modules_know_the_proposal_types`
/ `::test_only_the_use_case_turns_a_proposal_into_scene_overrides`
/ `::test_only_the_use_case_derives_edit_command_ids`
/ `tests/unit/test_application_slice.py::test_http_and_internal_port_give_the_same_result_shape_and_change_sets`
/ `::test_the_assistant_tool_gives_the_same_result_shape_and_change_sets`
/ `::test_resending_the_same_request_returns_the_existing_commands`
/ `::test_a_retried_tool_call_returns_the_existing_commands`
/ `::test_the_same_request_id_with_other_content_is_command_id_reused`
/ `::test_unsupported_fields_give_unsupported_change_from_every_entry`
/ `::test_the_tool_reports_the_same_refusal_codes_as_the_other_entries`
/ `::test_missing_propose_scope_is_missing_scope`
/ `::test_the_internal_port_refuses_a_human_caller`
/ `::test_the_flag_off_is_404_feature_disabled_before_anything_else`
/ `::test_a_stale_base_revision_ends_as_a_conflict_without_a_change_set`
/ `tests/unit/test_propose_storyboard_change.py::test_one_propose_command_per_edit_with_a_validated_payload`
/ `::test_unsupported_edits_are_refused_before_the_port`
/ `tests/contract/test_application_contracts.py::test_shared_error_codes_are_spelled_like_reject_reasons`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_one_episode_runs_every_stage_on_the_real_stack`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_replayed_run_adds_no_runs_reservations_artifacts_or_proposals`

### INV-51 定義の固定: 未知の Agent role・版は拒否し、登録済み spec は hash で固定され、計画は spec の版と hash を持つ
Agent は `contracts/agents.py::AGENT_SPECS`（`(role, spec_version)`）にだけ登録し、`resolve_agent_spec` は未知の
role・版を `UnknownAgentSpecError` で拒否する（既存 3 役は Registry の対象外）。登録済み spec の内容は
`manifest_hash`（全項目の正規 JSON の sha256）を literal で固定するテストが守る — 変えるなら spec を書き換えず
新しい `spec_version` を足す。コンパイル済みの `WorkflowPlan` は agent stage ごとに `agent_spec_version` /
`agent_spec_hash` / `model_policy_ref` / `prompt_version` を持ち、`plan_hash` が全体を覆う（改ざんは model 検証で拒否）。
hash 不一致の停止は `run_agent_stage`（`workers/agents/activities.py`）が実装する: pin された
`(role, spec_version, manifest_hash)` で spec を解決し、不一致・未知の版は**非再試行**の `agent_spec_drift`
（何も保存せず LLM も呼ばない）。`pin_workflow_plan` は Episode に計画を1度だけ固定し（既にあればそれを返す）、
workflow は Activity の結果の計画だけを使う。
**機械検査**: `tests/contract/test_agent_registry.py::test_registered_spec_hashes_are_pinned`
/ `tests/contract/test_agent_registry.py::test_changing_any_field_changes_the_hash`
/ `tests/contract/test_agent_registry.py::test_unknown_role_or_version_is_rejected`
/ `tests/contract/test_agent_registry.py::test_every_new_role_has_a_v1_spec_and_a_role_spec`
/ `tests/contract/test_agent_registry.py::test_tools_handlers_and_policies_are_registered_vocabulary`
/ `tests/unit/test_workflow_template_compiler.py::test_the_agentic_plan_pins_every_agent_spec_version_and_hash`
/ `tests/unit/test_workflow_template_compiler.py::test_compilation_is_deterministic_and_the_hash_covers_the_agent_specs`
/ `tests/unit/test_workflow_template_compiler.py::test_a_tampered_plan_is_rejected_by_its_own_hash`
/ `tests/unit/test_agentic_pipeline_activities.py::test_a_pinned_plan_is_returned_unchanged_after_the_template_changes`
/ `tests/unit/test_agentic_workflow.py::test_the_pinned_plan_is_used_after_the_template_changes`
/ `tests/unit/test_agentic_workflow.py::test_a_drifted_agent_spec_stops_without_retrying`
/ `tests/unit/test_agentic_use_cases.py::test_start_rejects_a_legacy_or_unrunnable_template`
/ `tests/unit/test_agent_stage_activity.py::test_a_changed_spec_hash_is_a_non_retryable_drift`
/ `tests/unit/test_agent_stage_activity.py::test_an_unknown_pinned_spec_version_is_also_drift`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_pinned_plan_survives_a_changed_template`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_drifted_agent_spec_stops_with_one_attempt`

### INV-52 契約同期: OpenAPI・アシスタント tool schema・contracts の model が一致する
`ProposeStoryboardChangeRequest` / `StoryboardProposalResult`（`contracts/application.py`）が唯一の定義で、OpenAPI の
当該 route の request/response schema、`propose_storyboard_patch` の入力 schema（`StoryboardPatchProposal` から導出）、
固定した OpenAPI 断片 `docs/api/openapi.application.json`（`scripts/export-openapi.py --application` の生成結果）は
それと一致する。手書きで同じ形を再掲しない。
**機械検査**: `tests/contract/test_application_contract_sync.py::test_the_route_request_schema_is_the_request_model`
/ `::test_the_route_response_schema_is_the_result_model`
/ `::test_the_tool_input_schema_is_derived_from_the_proposal_model`
/ `::test_the_committed_openapi_fragment_equals_a_fresh_generation`
/ `::test_the_fragment_has_only_the_application_paths_and_the_referenced_schemas`

### INV-53 Template の安全性: 未登録 handler/agent、循環、必須工程の欠落、QA 上限超過、未実装 Template を起動しない
`domain/workflow_templates/compiler.py` は純粋関数で、`TemplateCompileError(code)` を返して拒否する:
`unknown_template`・`template_not_runnable`・`unknown_handler`・`unknown_agent_spec`・`handler_mismatch`・
`duplicate_stage`・`unknown_dependency`・`cycle`・`missing_required_stage`・`required_stage_order`・
`strategy_without_producer`・`qa_cycle_limit`（`HARD_MAX_QA_REPAIR_CYCLES = 2`）・`invalid_post_publish`。
必須の既存工程（script → storyboard → production → render → private_upload）は各 1 回・この順に（推移的に）依存する。
Template・Plan の契約に公開設定・予算上限・Schedule を表す項目は無い。Template は Python のデータで、YAML・DB・
利用者入力から作らない。
**機械検査**: `tests/unit/test_workflow_template_compiler.py::test_every_registered_template_is_structurally_valid_and_compiles_iff_runnable`
/ `tests/unit/test_workflow_template_compiler.py::test_unknown_template`
/ `tests/unit/test_workflow_template_compiler.py::test_unknown_handler`
/ `tests/unit/test_workflow_template_compiler.py::test_unknown_agent_spec`
/ `tests/unit/test_workflow_template_compiler.py::test_handler_mismatch`
/ `tests/unit/test_workflow_template_compiler.py::test_duplicate_stage`
/ `tests/unit/test_workflow_template_compiler.py::test_unknown_dependency`
/ `tests/unit/test_workflow_template_compiler.py::test_cycle`
/ `tests/unit/test_workflow_template_compiler.py::test_missing_required_stage`
/ `tests/unit/test_workflow_template_compiler.py::test_a_required_stage_appearing_twice_is_missing_required_stage`
/ `tests/unit/test_workflow_template_compiler.py::test_required_stage_order`
/ `tests/unit/test_workflow_template_compiler.py::test_strategy_without_producer`
/ `tests/unit/test_workflow_template_compiler.py::test_qa_cycle_limit`
/ `tests/unit/test_workflow_template_compiler.py::test_invalid_post_publish`
/ `tests/contract/test_workflow_templates.py::test_no_template_or_plan_field_can_express_visibility_budget_or_schedule`
/ `tests/contract/test_workflow_templates.py::test_shorts_agentic_has_exactly_the_adr_stage_sequence`
/ `tests/contract/test_agentic_contracts.py::test_user_brief_has_no_budget_visibility_or_schedule_field`

**tool 版 Template（ADR-0047）**: `shorts_agentic_tools@1` は strategy/learning を 1.1、Historian を台本の後・storyboard の前に
置く。他の Template の hash と `shorts_agentic@1` の `plan_hash` は literal で固定され、変わらない。
**機械検査**: `tests/contract/test_workflow_template_agent_tools.py::test_existing_templates_and_the_old_plan_hash_are_unchanged`
/ `tests/contract/test_workflow_template_agent_tools.py::test_the_tool_template_has_exactly_the_adr_stage_sequence`
/ `tests/contract/test_workflow_template_agent_tools.py::test_the_tool_template_is_valid_and_compiles_to_a_plan_pinning_the_tool_specs`

### INV-54 保存の互換: 未知の schema 版は読まず、旧 Storyboard v1.0 は読め、migration 0019 は制約の張り替えだけ
`contracts/artifact_registry.py` の `(artifact_type, schema_version)` に無い版は `UnsupportedArtifactSchemaError`
（黙って別の版として読まない）。版をまたぐ読み替えは明示登録した upcaster だけで、本番の `UPCASTERS` は空。
保存済みの `storyboard@1.0` はこの registry を通して読める。migration 0019 は `ck_artifact_metadata_type`・
`ck_artifact_metadata_scene_scope`・`ck_agent_runs_role`・`ck_provider_reservations_provider`・
`ck_provider_auth_incidents_provider`・`ck_provider_rejections_provider` の張り替えだけ（表・列を作らない。語彙は literal で凍結し
contracts を import しない）。downgrade は新語彙の行があれば何も変えずに拒否する。
**機械検査**: `tests/contract/test_artifact_registry.py::test_an_unknown_schema_version_is_rejected_not_reinterpreted`
/ `tests/contract/test_artifact_registry.py::test_a_stored_storyboard_v1_is_readable_through_the_registry`
/ `tests/contract/test_artifact_registry.py::test_reading_across_versions_needs_an_explicitly_registered_upcaster`
/ `tests/contract/test_artifact_registry.py::test_the_registry_holds_the_adr_0043_entries_and_the_adr_0047_fact_check_report`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_frozen_literals_equal_the_current_enums_in_order`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_pre_0019_literals_are_the_values_at_9440302`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_the_migration_does_not_import_contracts`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_the_migration_creates_no_table_or_column`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_upgrade_and_downgrade_round_trip_on_sqlite`
/ `tests/contract/test_migration_0019_agentic_vocabulary.py::test_downgrade_refuses_and_changes_nothing_when_a_new_value_is_in_use`

### INV-55 既定オフ: 新機能の flag は既定で無効で、日次経路は agentic のモジュールを参照しない
`Settings` の `jpmas2_application_api`・`jpmas2_agentic_workflows` は既定 false、`jpmas2_enabled_agents` は空
（= 有効な role なし）、`jpmas2_agent_llm` は `codex`（`fake` は隔離試験専用）。未知の role 名は起動時に失敗する。
`workers/pipeline/workflows.py`（`EpisodePipelineWorkflow`）・`infrastructure/temporal/schedules.py` は
`contracts.agents`・`contracts.workflow_templates`・`contracts.agentic`・`domain.agents`・`domain.workflow_templates`・
`application` を直接 import しない。`workers/planning/**`（Topic Planner・Script）は同じ禁止に加え、
`contracts.agentic` からは `CreativeBrief` だけ（P1 の「TopicPlan が無く現行の creative_brief がある Episode」の分岐用）を
許す。検査は直接の import（AST）で、動的 import・推移的な依存は見ない。
**機械検査**: `tests/unit/test_agentic_flags.py::test_every_agentic_flag_defaults_to_off`
/ `tests/unit/test_agentic_flags.py::test_enabled_agents_are_parsed_from_a_comma_separated_list`
/ `tests/unit/test_agentic_flags.py::test_an_unknown_agent_name_fails_at_load_with_the_known_names`
/ `tests/unit/test_agentic_flags.py::test_the_agent_llm_is_codex_or_fake_only`
/ `tests/architecture/test_agentic_default_off.py::test_daily_path_modules_import_nothing_agentic`
/ `tests/architecture/test_agentic_default_off.py::test_the_planning_worker_may_only_read_the_creative_brief`
/ `tests/architecture/test_agentic_default_off.py::test_the_checker_catches_each_import_form_and_checks_allowed_names`
/ `tests/unit/test_agentic_worker_registration.py::test_the_default_registration_is_exactly_the_daily_workflows_and_activities`
/ `tests/unit/test_agentic_worker_registration.py::test_main_builds_the_agentic_activities_only_behind_the_flag`
/ `tests/unit/test_agentic_workflow.py::test_legacy_histories_still_replay_with_the_agentic_workflows_registered`
/ `tests/unit/test_agentic_workflow.py::test_a_history_produced_by_the_legacy_workflow_replays`
/ `tests/unit/test_agentic_workflow.py::test_the_legacy_workflow_module_does_not_mention_the_agentic_workflow`
/ `tests/unit/test_script_creative_brief.py::test_an_episode_without_a_creative_brief_keeps_the_legacy_brief_and_hash`
/ `tests/unit/test_script_creative_brief.py::test_an_episode_with_a_plan_keeps_the_plan_brief_even_with_a_creative_brief`
/ `tests/unit/test_agentic_use_cases.py::test_start_is_refused_when_the_flag_is_off`
/ `tests/unit/test_agentic_cli.py::test_start_is_refused_when_the_flag_is_off`
/ `tests/unit/test_agent_tools_start_gate.py::test_the_tool_template_is_refused_with_feature_disabled_when_tools_are_off`
/ `tests/unit/test_agent_tools_start_gate.py::test_the_existing_template_does_not_need_the_tools_flag`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_the_default_pipeline_worker_is_unchanged_and_a_legacy_run_writes_nothing_agentic`

### INV-56 Agent は Tool Gateway が許した tool だけを上限内で呼び、LLM 費用の欠測を 0 にせず、予約・予算 gate を通ってから呼ぶ
handler は `ToolGateway` 経由でしか外部を呼ばない。`spec.allowed_tools` に無い tool・供給されていない tool は
何かを呼ぶ前に `ToolNotAllowedError`、LLM 呼び出しが `spec.max_llm_calls` を超えれば `AgentBudgetExceededError`。
記録（`tool_invocations`）は prompt 本文を含まない。実績費用は token usage × policy 単価で、usage か単価が欠ければ
`None`（`token_usage`・`actual_cost_usd`・`agent_runs.estimated_cost_usd` とも NULL。0 にしない）。LLM 呼び出しの前に
`ProviderCall.AGENT_LLM` を `spec.max_estimated_cost_usd`（上限）で予約して commit し、最初の予約の前にだけ
`budget_view` + `check_additional_cost` を検査する（換算率なし = `budget_unverifiable`、超過 = `budget_exceeded`。
どちらも予約を作らない）。dispatch は gate を通り、hold 中は `EpisodeHeldError` で予約は未 dispatch のまま残る。
再送は同じ予約を返し再検査しない。Agent stage（`run_agent_stage`）はこの gate を handler の前に通し、拒否・停止は
`blocked`（`budget_*` / `held`）で返して成果物を作らず、呼んだ事実は検証より前に台帳へ確定する。前の試行が
呼び終えていれば保存済みの応答を再生し、LLM を呼び直さない。台帳の key は列（VARCHAR(64)）に収まるよう
`agent_reservation_key`（冪等キーの SHA-256 の先頭 40 桁）に写す。
**機械検査**: `tests/unit/test_agent_tool_gateway.py::test_an_unlisted_tool_is_refused_before_anything_is_called`
/ `tests/unit/test_agent_tool_gateway.py::test_a_listed_but_unprovided_tool_is_refused`
/ `tests/unit/test_agent_tool_gateway.py::test_the_llm_call_cap_is_enforced_before_the_next_call`
/ `tests/unit/test_agent_tool_gateway.py::test_unknown_usage_makes_tokens_and_cost_none_not_zero`
/ `tests/unit/test_agent_tool_gateway.py::test_an_unpriced_policy_gives_none_cost_but_known_tokens`
/ `tests/unit/test_agent_tool_gateway.py::test_invocations_are_json_safe_and_never_contain_the_prompt`
/ `tests/unit/test_agent_tool_gateway.py::test_unknown_usage_gives_none_not_zero`
/ `tests/unit/test_agent_budget_gate.py::test_the_first_call_reserves_the_ceiling_then_dispatches`
/ `tests/unit/test_agent_budget_gate.py::test_without_a_usd_jpy_rate_the_call_is_refused_before_any_reservation`
/ `tests/unit/test_agent_budget_gate.py::test_exceeding_the_episode_budget_is_refused_before_any_reservation`
/ `tests/unit/test_agent_budget_gate.py::test_a_held_episode_refuses_dispatch_and_the_reservation_stays_undispatched`
/ `tests/unit/test_agent_budget_gate.py::test_a_replay_returns_the_same_reservation_without_a_second_budget_check`
/ `tests/unit/test_agent_run_cost_capture.py::test_an_unknown_cost_stays_null_not_zero`
/ `tests/unit/test_agent_run_cost_capture.py::test_a_known_cost_and_the_agent_spec_are_stored`
/ `tests/unit/test_agent_stage_activity.py::test_a_budget_refusal_leaves_no_reservation_and_no_artifact`
/ `tests/unit/test_agent_stage_activity.py::test_an_exceeded_budget_blocks_with_budget_exceeded`
/ `tests/unit/test_agent_stage_activity.py::test_a_held_episode_is_blocked_with_an_undispatched_reservation`
/ `tests/unit/test_agent_stage_activity.py::test_a_disabled_agent_is_blocked_not_silently_skipped`
/ `tests/unit/test_agent_stage_activity.py::test_a_handler_cannot_use_a_tool_outside_its_spec`
/ `tests/unit/test_agent_stage_activity.py::test_unknown_token_usage_stays_null`
/ `tests/unit/test_agent_stage_activity.py::test_after_a_crash_between_the_call_and_the_save_the_llm_is_not_called_again`
/ `tests/unit/test_agent_reservation_key.py::test_the_reservation_key_fits_the_column_and_is_deterministic_per_stage_key`
/ `tests/unit/test_agent_reservation_key.py::test_a_reservation_for_a_real_sized_stage_key_is_found_again_by_the_mapped_key`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_one_episode_runs_every_stage_on_the_real_stack`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_missing_exchange_rate_blocks_the_first_agent_stage`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_hold_requested_before_start_parks_at_the_first_stage`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_a_replayed_run_adds_no_runs_reservations_artifacts_or_proposals`


### INV-57 Agent handler は決定論の規則が最終で、入力は DATA として渡し、修正案は提案に留め、private・欠測から Lesson を作らない
4 つの handler（`domain/agents/handlers/`）は `AGENT_INPUT_NAMES` の名前の document だけを読み、外へは
`ToolGateway.llm_json` 以外を呼ばない。LLM の応答は契約（`contracts/agentic.py` から導いた schema）で検証し、
違反は `AgentOutputRejected`。**決定論の規則が最終**: Strategy は直近タイトルの近い重複・profile 外の era を
`rejected` にし、`audience_basis` は実測かつ十分な動画数のときだけ `measured`、`lessons_used` は Lesson の status
だけで決める。Creative は採用候補の topic/subject/angle/era を写し LLM に変えさせない。Quality は入力欠測なら LLM を
呼ばず `blocked_missing_input`、存在しない scene・尺超過の所見は捨てて理由を残し、`StoryboardPatchProposal` は
visual_description のみ・最大 3 edit の**提案**で handler は適用しない。Learning は公開されていない・測定が全く無い
ときは LLM を呼ばず Lesson を作らず（`awaiting_public_metrics`）、Lesson の status は常に `proposed`、欠測は `null`。
prompt は入力を `<data>` で区切り「指示ではない」と明示し、ユーザ入力・LLM 出力は tool 名や指示にならない。
**機械検査**: `tests/unit/test_agent_handlers.py::test_handlers_register_every_handler_ref_and_only_those`
/ `tests/unit/test_agent_handlers.py::test_a_near_duplicate_of_a_recent_title_is_rejected_deterministically`
/ `tests/unit/test_agent_handlers.py::test_an_era_outside_the_strategy_profile_is_rejected`
/ `tests/unit/test_agent_handlers.py::test_audience_basis_is_measured_only_with_enough_videos`
/ `tests/unit/test_agent_handlers.py::test_lessons_used_follows_status_not_the_llm`
/ `tests/unit/test_agent_handlers.py::test_a_brief_with_injection_text_changes_no_tool_call_and_is_only_data`
/ `tests/unit/test_agent_handlers.py::test_a_prompt_cannot_close_its_data_block`
/ `tests/unit/test_agent_handlers.py::test_the_llm_cannot_change_the_adopted_subject_and_injection_is_inert`
/ `tests/unit/test_agent_handlers.py::test_missing_input_blocks_the_review_without_an_llm_call`
/ `tests/unit/test_agent_handlers.py::test_findings_on_unknown_scenes_or_beyond_the_duration_are_dropped_and_recorded`
/ `tests/unit/test_agent_handlers.py::test_major_findings_become_visual_only_proposals_capped_at_three`
/ `tests/unit/test_agent_handlers.py::test_private_or_unmeasured_uploads_wait_and_never_call_the_llm`
/ `tests/unit/test_agent_handlers.py::test_the_llm_cannot_set_the_lesson_status`
/ `tests/unit/test_agent_handlers.py::test_shorts_and_long_form_baselines_are_not_mixed`
/ `tests/unit/test_agent_handlers.py::test_a_strategy_llm_output_violating_the_contract_is_rejected`
/ `tests/unit/test_agent_stage_activity.py::test_quality_proposals_arrive_as_proposed_agent_change_sets_and_are_not_applied`
/ `tests/unit/test_agent_stage_activity.py::test_the_agent_caller_never_holds_the_apply_scope`
/ `tests/unit/test_agent_stage_activity.py::test_an_awaiting_learning_stage_writes_no_lesson`
/ `tests/unit/test_agent_stage_activity.py::test_a_tool_queued_proposal_is_submitted_after_the_save_and_not_applied`
/ `tests/unit/test_agent_stage_activity.py::test_a_replay_resubmits_proposals_idempotently`
/ `tests/unit/test_agent_stage_activity.py::test_real_handlers_run_against_assembled_inputs_for_every_role`
/ `tests/unit/test_agent_stage_scope.py::test_a_later_run_with_a_new_scope_can_produce_the_lesson_after_an_awaiting_run`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_one_episode_runs_every_stage_on_the_real_stack`
/ `tests/integration/test_jpmas2_agentic_e2e.py::test_learning_waits_for_public_metrics_then_proposes_one_lesson`

**tool 版（ADR-0047、spec 1.1 / historian@1.0）**: `spec.allowed_tool_ids` が空でない handler は
`domain/agents/handlers/tool_use.py` の**1 往復**（plan の LLM 1 回 → 検査して逐次実行 → 合成の LLM 1 回 +
契約違反の修復 1 回。計 3 回以下）だけで tool を使い、1.0 の経路・prompt は変わらない。許可外・未登録・入力違反・上限超過・重複・
handler の入力方針（Learning は当該動画だけ、Historian は ≤12 件の検査済み claim だけ）に反する要求は**外部を呼ばずに**捨て、理由を残す。
tool の失敗・未完・権限不足は uncertainties / limitations になり、値を作らない。引用は `tool_call:<id>` で、この run に
実在しない id は本文・引用列から決定論で除く（StrategyBrief は `citations` と判断の `evidence_refs`、Lesson は本文中の引用と
判断の `evidence_refs`、FactCheckReport は claim ごとの `evidence_refs`）。`audience_basis` は analytics の実測だけ、Learning は
private・欠測なら LLM も tool も呼ばず、Historian の `assessment` は tool の語をそのまま写す（知らない語は `insufficient`）。
**機械検査**: `tests/unit/test_agent_tool_use.py::test_tool_results_can_never_cause_another_tool_call`
/ `tests/unit/test_agent_tool_use.py::test_one_repair_call_fits_in_the_three_call_budget`
/ `tests/unit/test_agent_handlers_tools_strategy.py::test_a_tool_round_produces_a_valid_brief_citing_the_executed_tool_calls`
/ `tests/unit/test_agent_handlers_tools_strategy.py::test_unknown_tool_call_citations_are_dropped_and_recorded`
/ `tests/unit/test_agent_handlers_tools_strategy.py::test_audience_basis_never_comes_from_the_llm_or_from_trend_hypotheses`
/ `tests/unit/test_agent_handlers_tools_strategy.py::test_v1_0_never_plans_even_if_a_tool_runtime_exists`
/ `tests/unit/test_agent_handlers_tools_learning.py::test_private_or_unmeasured_uploads_wait_without_llm_or_tools`
/ `tests/unit/test_agent_handlers_tools_learning.py::test_tools_are_limited_to_the_uploaded_video`
/ `tests/unit/test_agent_handlers_tools_learning.py::test_unknown_citations_are_removed_and_uncited_hypotheses_are_dropped`
/ `tests/unit/test_agent_handlers_tools_historian.py::test_assessments_are_passed_through_as_is_with_evidence_links`
/ `tests/unit/test_agent_handlers_tools_historian.py::test_an_unknown_assessment_is_insufficient_not_mapped_to_a_guess`
/ `tests/unit/test_agent_handlers_tools_historian.py::test_a_missing_script_blocks_without_llm_or_tools`


### INV-58 Tool は許可された `tool_id@version` だけを、宣言した上限内で呼び、取得できない値を架空の値で埋めない
Agent は `AgentSpec.allowed_tool_ids`（`"search_trends@1"` の形）にある tool だけを `ToolGateway.call_tool` で呼べる。
許可外・未登録・未供給（`JPMAS2_ENABLED_TOOLS` に無い）・入力が `ToolDef.input_model` に合わない・`max_calls_per_run`
超過は、**外部を呼ぶ前**に拒否し（`ToolNotAllowedError` / `ToolInputInvalidError` / `ToolCallLimitExceededError`、
`tool_invocations` に `refused` で記録、`agent_tool_calls` の行は作らない）、`agent.tool.refused` を出す。
`canonical()` は空の `allowed_tool_ids` を出さないので登録済み 1.0 spec の `manifest_hash` は変わらない（INV-51）。
結果は例外ではなく `ToolStatus`（データ無し・標本不足・privacy 閾値・反映遅延・API 非対応・権限不足・quota・
timeout・未完・Provider 未設定・非対応・失敗）で返し、`data` は `ok` のときだけ。executor が契約に違反した出力
（`output_model` 違反、ok なのに data 無し、非 ok なのに data あり）は `failed` にし、壊れた値を渡さない。
timeout は `asyncio.wait_for` で `timeout`、executor の例外は `failed`（コードは型名のみ。本文を残さない）。
**機械検査**: `tests/unit/test_agent_tool_call_gateway.py::test_a_tool_outside_the_spec_is_refused_before_anything_external_runs`
/ `tests/unit/test_agent_tool_call_gateway.py::test_a_tool_not_in_this_agents_list_is_refused_even_if_registered`
/ `tests/unit/test_agent_tool_call_gateway.py::test_an_allowed_tool_missing_from_the_registry_is_unknown`
/ `tests/unit/test_agent_tool_call_gateway.py::test_a_tool_that_is_not_enabled_has_no_executor_and_is_refused`
/ `tests/unit/test_agent_tool_call_gateway.py::test_invalid_input_is_refused_without_an_external_call_or_a_row`
/ `tests/unit/test_agent_tool_call_gateway.py::test_max_calls_per_run_is_enforced_per_tool`
/ `tests/unit/test_agent_tool_call_gateway.py::test_a_timeout_becomes_a_timeout_status`
/ `tests/unit/test_agent_tool_call_gateway.py::test_an_executor_exception_is_a_failed_status_without_its_message`
/ `tests/unit/test_agent_tool_call_gateway.py::test_output_that_violates_the_output_model_is_failed_not_passed_through`
/ `tests/unit/test_agent_tool_call_gateway.py::test_data_without_ok_or_ok_without_data_is_an_executor_contract_violation`
/ `tests/unit/test_agent_tool_call_gateway.py::test_states_pass_through_unchanged_and_carry_no_data`
/ `tests/contract/test_agent_spec_tools.py::test_v1_0_specs_have_no_tools_and_omit_the_key_from_canonical`
/ `tests/contract/test_agent_spec_tools.py::test_the_new_specs_carry_exactly_the_adr_tool_lists`
/ `tests/contract/test_agent_registry.py::test_registered_spec_hashes_are_pinned`
/ `tests/contract/test_agent_tools_contract.py::test_status_vocabulary_distinguishes_the_four_failure_kinds`
/ `tests/unit/test_agent_tool_call_repository.py::test_complete_stores_the_body_in_the_object_store_and_only_a_reference_in_the_row`

### INV-59 Tool の外部要求は Activity の retry で重複しない
tool 呼び出しは `tool_call_id = uuid5(run の冪等キー : seq : tool_id : 入力の sha256)` を持ち、実行前に `agent_tool_calls`
へ `started` で commit し、完了で結果（本体は MinIO、行には参照と sha256）を保存する。retry は同じ ID の保存済みの
結果を返し、**executor を呼ばない**（ok 以外の状態も再利用する）。`started` のまま結果が無い行（呼んだか不明）は、
`youtube_quota` の tool では**再送せず** `failed`（`ambiguous_prior_attempt`）を確定させ、`research_ledger` の tool
では同じ `idempotency_key = tool:{tool_call_id}` で再実行する（Research の台帳が二重課金を防ぐ）。保存済みの結果が行の
sha256 と合わなければ `ToolCallIntegrityError`。同じ `(agent_run_key, seq)` に別の ID は入らない（UNIQUE）。
migration 0020 は新しい語彙・`agent_tool_calls` の行があれば downgrade を拒否する。
**機械検査**: `tests/unit/test_agent_tool_call_gateway.py::test_the_id_and_seq_are_deterministic_and_come_from_the_contract`
/ `tests/unit/test_agent_tool_call_gateway.py::test_a_retry_returns_the_stored_result_and_never_calls_the_executor`
/ `tests/unit/test_agent_tool_call_gateway.py::test_a_stored_non_ok_result_is_also_reused`
/ `tests/unit/test_agent_tool_call_gateway.py::test_an_ambiguous_youtube_quota_attempt_is_not_called_again`
/ `tests/unit/test_agent_tool_call_gateway.py::test_an_unfinished_research_call_is_reinvoked_with_the_same_idempotency_key`
/ `tests/unit/test_agent_tool_call_repository.py::test_begin_commits_a_started_row_with_no_result`
/ `tests/unit/test_agent_tool_call_repository.py::test_begin_twice_with_the_same_id_is_a_no_op`
/ `tests/unit/test_agent_tool_call_repository.py::test_two_different_ids_cannot_take_the_same_run_and_seq`
/ `tests/unit/test_agent_tool_call_repository.py::test_a_tampered_result_is_detected`
/ `tests/unit/test_agent_tool_call_repository.py::test_a_gateway_retry_on_the_real_repository_never_calls_the_executor_again`
/ `tests/unit/test_agent_tool_call_repository.py::test_an_ambiguous_youtube_quota_row_on_the_real_repository_is_not_resent`
/ `tests/unit/test_agent_tools_wiring.py::test_a_strategy_1_1_stage_calls_a_tool_once_and_a_replay_does_not`
/ `tests/unit/test_agent_tools_wiring.py::test_a_stage_retry_before_the_stage_was_saved_reuses_the_stored_tool_result`
/ `tests/contract/test_migration_0020_agent_tool_calls.py::test_downgrade_refuses_and_changes_nothing_when_new_data_is_in_use`
/ `tests/contract/test_agent_tools_contract.py::test_tool_call_id_is_deterministic_and_depends_on_every_part`

### INV-60 外部由来の文字列は untrusted で、tool 名・指示・URL の生成に使わず、tool は 1 stage 1 往復で再帰できない
`ToolEnvelope.untrusted` は常に `True`。LLM へは `render_untrusted`（handler の prompt と同じ `<data>` 区切りで `<` を
エスケープする `domain/agents/data_block.py`）で渡し、指示として扱わせない。LLM が作った tool 名は
`allowed_tool_ids` に無ければ `ToolNotAllowedError`（綴り不正・版なし・未知の名前も拒否）。tool を使う spec は
`max_llm_calls <= 3`（plan → execute → synthesize の 1 往復。結果からの追加要求は構造上できない）。`FactCheckReport`
も `untrusted=True`。`JPMAS2_AGENT_TOOLS` 無効の gateway は全ての `call_tool` を拒否する。
**機械検査**: `tests/unit/test_agent_tool_call_gateway.py::test_render_untrusted_wraps_the_envelope_in_a_data_block_and_escapes_delimiters`
/ `tests/unit/test_agent_tool_call_gateway.py::test_render_untrusted_reuses_the_handler_data_block`
/ `tests/unit/test_agent_tool_call_gateway.py::test_garbage_refs_are_refused`
/ `tests/unit/test_agent_tool_call_gateway.py::test_without_a_tool_runtime_every_tool_call_is_refused`
/ `tests/contract/test_agent_spec_tools.py::test_tool_specs_stay_within_one_round_trip_of_llm_calls`
/ `tests/contract/test_agent_tools_contract.py::test_envelope_data_is_present_only_for_ok_and_always_untrusted`
/ `tests/contract/test_fact_check_report.py::test_the_report_is_strict_and_untrusted`
/ `tests/unit/test_agent_tools_wiring.py::test_without_tool_support_a_tool_call_is_refused_and_no_row_is_written`
/ `tests/unit/test_agent_tools_wiring.py::test_a_1_0_stage_never_gets_tools_even_when_tool_support_is_wired`

### INV-61 Tool は read-only
`ToolEffect` は `read` だけで、`ToolDef` は他の effect を作れない。tool の executor を置く
`infrastructure/agents/tools/` は書き込み scope（投稿・force-ssl・収益・メンバーシップ）の文字列も
POST/PUT/DELETE/PATCH の呼び出しも持たない。投稿・削除・更新・再投稿の tool は存在しない（AGENTS.md §9）。
Analytics の拡張実装は read 用の port 越しにだけ使う（既存の `infrastructure/analytics/youtube_analytics.py` は
Planner 用の既存 adapter で、この検査の対象外）。
**機械検査**: `tests/contract/test_agent_tools_contract.py::test_only_read_tools_can_be_defined`
/ `tests/architecture/test_agent_tools_boundaries.py::test_tool_implementations_have_no_write_scope_or_write_http_method`
/ `tests/architecture/test_agent_tools_boundaries.py::test_the_write_check_catches_each_form`

### INV-62 Tool の定義は `contracts/agent_tools.py` の 1 か所
tool の識別子・状態・入出力の包みは `contracts/agent_tools.py`、登録は `contracts/agent_tool_registry.py`。JSON Schema は
model から導出し手書きしない。apps / workers / infrastructure / domain / application は tool 識別子を文字列 literal で
書かず `ToolId` を import する。`agent_tool_calls.status` の語彙（`TOOL_CALL_STATUSES`）・`AgentRole`・`ArtifactType` は
migration 0020 の凍結 literal と一致する。同じ `(tool_id, version)` の二重登録・同じ tool を二つの executor 群が登録
することは起動時に失敗する。flag（`JPMAS2_AGENT_TOOLS` / `JPMAS2_ENABLED_TOOLS`）は既定で無効で、日次経路は tool の
モジュールを参照しない（INV-55 の延長）。
**機械検査**: `tests/contract/test_agent_tools_contract.py::test_json_schema_is_derived_from_the_models_not_written_by_hand`
/ `tests/contract/test_agent_tools_contract.py::test_the_eight_tool_ids_are_the_requested_vocabulary`
/ `tests/contract/test_agent_tools_contract.py::test_the_registry_rejects_duplicate_registration`
/ `tests/architecture/test_agent_tools_boundaries.py::test_tool_ids_are_not_written_as_literals_outside_contracts`
/ `tests/architecture/test_agent_tools_boundaries.py::test_the_literal_check_catches_a_hardcoded_tool_id`
/ `tests/architecture/test_agent_tools_boundaries.py::test_the_tool_registry_and_domain_ports_stay_pure`
/ `tests/contract/test_migration_0020_agent_tool_calls.py::test_frozen_literals_equal_the_current_contracts_in_order`
/ `tests/unit/test_agent_tools_wiring.py::test_tool_flags_default_to_off`
/ `tests/unit/test_agent_tools_wiring.py::test_an_unknown_tool_id_fails_at_load_with_the_known_ids`
/ `tests/unit/test_agent_tools_wiring.py::test_the_same_tool_from_two_groups_is_a_startup_error`
/ `tests/unit/test_agent_tools_wiring.py::test_no_tool_support_is_built_when_the_flag_is_off`
/ `tests/architecture/test_agentic_default_off.py::test_daily_path_modules_import_nothing_agentic`
