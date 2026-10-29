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

## M. リリース（ADR-0050）

### INV-65 リリース判定は三値で、確認できなかったものを READY にせず、判定自体は何も変更しない
`scripts/release-preflight.py` の総合判定は、BLOCKED のチェックが1つでもあれば BLOCKED、なければ
UNKNOWN のチェックまたは欠けた必須チェックが1つでもあれば UNKNOWN、必須が全て PASS のときだけ READY。
終了コードは READY=0 / BLOCKED=2 / UNKNOWN=3。collector の例外・不在は UNKNOWN になり、READY にならない。
判定は読み取りだけを行い（書き込み・再起動・Schedule 変更・有料 API・YouTube を呼ばない）、
新しい migration は expand-only（破壊的変更は `# contract-migration: approved ADR-NNNN` の印が無ければ BLOCKED。
印はコメント token のみ有効で、ADR が deploy 済み revision に在り Accepted で revision id に言及すること。
静的に読めない書き方は UNKNOWN、本番に入った migration の書き換えは BLOCKED）。
**この走査は best effort で、通過は安全の証明ではない。人のレビューは省けない**。
承認・証跡は SHA・日付（未来/72h 超は BLOCKED）・preflight 出力の sha256 に束縛する（自己申告で認証ではない）。
rollback は `--rollback` が「DB が先・expand-only・owner の旧コード互換記録」を判定し、migrate は走らせない。
判定の宛先（DB・Temporal namespace）が staging / `_test` DB なら BLOCKED（`target_environment`）。
rollback は alembic downgrade に依存しない。staging（`compose.staging.yaml`）は本番と project 名・volume・
bind path・host port・Temporal namespace を共有せず、FAL / YouTube の資格情報は空。
**機械検査**: `tests/unit/test_release_preflight.py::test_unknown_never_maps_to_ready`
/ `tests/unit/test_release_preflight.py::test_missing_required_check_is_unknown_not_ready`
/ `tests/unit/test_release_preflight.py::test_exit_codes`
/ `tests/unit/test_release_preflight.py::test_collector_exception_becomes_unknown`
/ `tests/unit/test_release_preflight.py::test_destructive_migration_is_blocked_without_marker`
/ `tests/unit/test_release_migration_scan.py`（bypass サンプル・UNKNOWN・in-place 編集・marker なりすまし）
/ `tests/unit/test_release_preflight.py::test_staging_or_test_targets_are_blocked`
/ `tests/unit/test_release_collectors_target.py`
/ `tests/unit/test_release_approval_and_rollback.py`（承認の鮮度・束縛、rollback の各分岐）
/ `tests/unit/test_deploy_scripts.py::test_skip_migrate_runs_no_migration_and_ignores_the_old_migrate_container`
/ `tests/architecture/test_release_preflight_read_only.py`
/ `tests/contract/test_compose_staging.py`

## N. バックアップと復旧（ADR-0053）

### INV-66 バックアップは「復旧演習に通るまで未検証」で、復旧は隔離先にしか書かない
`scripts/backup.py run` が作る manifest は常に `restore_verified: false`。`true` にできるのは
`scripts/backup.py restore-drill` だけで、それも全検査（全ファイルの sha256・3 DB の復元・行数が backup 時の
`[before, after]` に入る・alembic revision・オブジェクトの sha256・DB の `artifact_metadata` /
`research_artifacts` の全行が指す本体の存在と sha256）が pass の証跡があるときだけ（`apply_drill`）。
backup は本番の PostgreSQL へ `pg_dump` / `pg_dumpall --globals-only` / `SELECT` だけ、MinIO へ list / get だけを
送り、PostgreSQL を先、MinIO を後に取る（Artifact は不変で DB 行は本体の put の後に commit されるため、dump の
参照先は後から取る写しに存在する）。保存先は環境変数で明示され（既定なし）、守る対象と同じデバイスなら拒否。
復旧先（container / compose project / host / port / DB 名 / bucket 名 / 作業パス）が本番または backup root と
1 つでも一致したら、何も実行せずに拒否する。保持の pruner は未検証・未完了の集合と、最新の検証済み 1 つを
決して消さない。外部送信（offsite）と通知は既定オフ。
**機械検査**: `tests/unit/backup/test_domain.py::test_only_a_fully_passing_drill_flips_restore_verified`
/ `tests/unit/backup/test_domain.py::test_restore_refuses_any_production_identifier`
/ `tests/unit/backup/test_restore_drill.py::test_restore_refuses_a_production_target_before_running_anything`
/ `tests/unit/backup/test_restore_drill.py::test_a_corrupted_backup_file_is_detected_and_nothing_is_restored`
/ `tests/unit/backup/test_domain.py::test_retention_never_deletes_the_last_verified_backup`
/ `tests/unit/backup/test_prune.py::test_unverified_backups_are_never_pruned_even_far_outside_the_policy`
/ `tests/unit/backup/test_backup_service.py::test_destination_on_the_production_device_is_refused`
/ `tests/unit/backup/test_backup_service.py::test_size_mismatch_and_etag_mismatch_are_detected`
/ `tests/contract/test_backup_contracts.py::test_a_real_manifest_passes_the_release_preflight_backup_check`
/ `tests/architecture/test_backup_boundaries.py`（本番へは dump と SELECT だけ・削除と docker run/rm の置き場・
`restore_verified=True` を書けるのは `apply_drill` だけ・外部送信は既定オフ）

## N. 未照合予約（ADR-0053）

### INV-67 確定済み・未確定の課金予約を、新しい idempotency key で再送しない（ADR-0053）
provider が成功を確認した（`confirmed_success`）予約、または未照合で `unknown` / `still_running`
の予約、または**照合記録が無くても `dispatched_at` のある `reserved`**（呼んだ可能性がある。`unknown` として扱う）
がある入力（Episode・provider・シーン・`input_hash`）へ、`reserve` は新しい予約を作らず
`ResubmitRejectedError` を投げる（別の idempotency key の場合。同じ key は対象外）。未 dispatch の `reserved`・
`spent` / `abandoned` / `confirmed_failed` / `cancelled` は従来どおり。`unknown` は失敗ではなく、観測できなかったことを理由に失敗扱い・再送にしない。
判定は追記専用の `provider_reservation_reconciliations` に残り、確定は他の確定へ変わらず、
同じ観測の再実行は行を増やさない。人手確定は `--evidence` と `--owner-approval`（`OWNER-<yyyymmdd>-<id>` 形式。認証ではない）が必須で、承認 token は
指紋だけを保存する。`scripts/reconcile-reservations.py` は provider へ接続せず、`reconcile` / `resolve` は既定 dry-run。
**機械検査**: `tests/unit/test_reservation_reconciliation_repo.py::test_confirmed_success_can_never_be_resubmitted_under_a_new_key`
/ `tests/unit/test_reservation_reconciliation_repo.py::test_unknown_reservation_can_never_be_resubmitted_under_a_new_key`
/ `tests/unit/test_reservation_reconciliation_repo.py::test_dispatched_reserved_without_any_verdict_blocks_a_new_key`
/ `tests/unit/test_reservation_ledger_locking.py::test_resolve_manual_locks_the_reservation_row`
/ `tests/unit/test_reservation_ledger_locking.py::test_reserve_takes_the_input_advisory_lock_only_on_postgresql`
/ `tests/unit/test_reservation_reconciliation_domain.py::test_advance_is_idempotent`
/ `tests/unit/test_reservation_reconciliation_domain.py::test_unknown_stays_unknown_without_new_evidence`
/ `tests/unit/test_reservation_reconciliation_domain.py::test_manual_resolution_requires_evidence_and_owner_approval`
/ `tests/unit/test_reservation_reconciliation_domain.py::test_owner_approval_must_be_a_structured_reference`
/ `tests/unit/test_reconcile_reservations_script.py::test_resolve_requires_evidence_and_owner_approval`
/ `tests/unit/test_reconcile_reservations_script.py::test_reconcile_is_dry_run_by_default_and_writes_nothing`
/ `tests/unit/test_reconcile_reservations_script.py::test_script_has_no_network_or_provider_imports`

## O. 日次 Workflow の失敗検知（ADR-0054）

### INV-68 Episode が作られない日次 workflow の失敗を、健全として黙って見過ごさない（ADR-0054）
直近 N 日（`daily_failure_lookback_days`）の slot 日のうち、日次 workflow が Failed / TimedOut /
Terminated / Canceled で閉じ、かつその日の slot（= Episode）が無い日は `DAILY_WORKFLOW_FAILED`
として `operational_anomalies` に残る。終了状態を確認できない日（Temporal を読めない・保持期間切れ）は
`DAILY_WORKFLOW_UNOBSERVABLE` として残り、**確認できなかったことを OK にしない**。
Completed / Running の実行がある日と slot がある日は異常にしない。1日1行（hourly 再実行で行は増えず、
窓の外の日付を新規に作らない）、確定した FAILED は UNOBSERVABLE に劣化させない。detail は失敗の型と
message の要約（secret・UUID・URL query を伏せ、長さを制限）と `consecutive_failures` だけを持つ。
**機械検査**: `tests/unit/test_daily_failure_domain.py::test_failed_without_slot_is_failed`
/ `tests/unit/test_daily_failure_domain.py::test_completed_or_running_is_ok`
/ `tests/unit/test_daily_failure_domain.py::test_unreadable_is_unobservable_never_ok`
/ `tests/unit/test_daily_failure_domain.py::test_summary_redacts_secrets_and_truncates`
/ `tests/unit/test_daily_workflow_failure_watchdog.py::test_failed_yesterday_and_today_records_two_consecutive_days`
/ `tests/unit/test_daily_workflow_failure_watchdog.py::test_rerun_adds_no_rows_and_is_not_new`
/ `tests/unit/test_daily_workflow_failure_watchdog.py::test_unreadable_temporal_records_unobservable_not_ok`
/ `tests/unit/test_daily_workflow_failure_watchdog.py::test_recorded_failure_survives_retention_expiry`
/ `tests/unit/test_daily_workflow_failure_watchdog.py::test_failure_outside_window_is_not_recorded`

## P. 破壊的スキーマ操作の隔離（ADR-0055）

### INV-69 `drop_all` / `create_all` / `DROP TABLE|SCHEMA|DATABASE` / `TRUNCATE` は、URL が接続先を完全に決めた `_test` DB にしか実行されず、静的判定は接続を作る前に行う（ADR-0055）
判定は `infrastructure/db/safety.py` の1か所。次の URL は `UnsafeDatabaseError`: DB名が `_test` で終わらない・
**host または DB 名が URL に無い**（libpq が `PGHOST` / `PGDATABASE` / `PGSERVICE` で決める）・
`PGHOSTADDR` / `PGSERVICE` / `PGSERVICEFILE` が環境にある・host が `localhost` / `127.0.0.1` / `::1` 以外
（`AVP_ALLOW_REMOTE_TEST_DB=1` 無し。**compose のサービス名 `postgres` と `0.0.0.0` はローカル扱いしない**）・
アプリの `DATABASE_URL` / `Settings().database_url` と同じDB（`postgres` / `0.0.0.0` も「同じサーバの別名」として
同一性の判定にだけ使い、host 別名・末尾ドット・DB名の大文字小文字・`?options` を畳んで比較）・query が接続先を
上書きする（`host` / `dbname` / `port` ...）。allowlist を渡しても、アプリのDBと同一のDBは通らない。
tests/support/db.py は再公開のみで規則を持たない。実際に繋がっている接続の検査は INV-83。
破壊的な呼び出しは **箇所ごと** に、囲む関数の中で先行するガード呼び出しを持つ（SQLite のインメモリだけを
自分で作る tests の関数を除く）。ファイルに1か所ガードがあれば他の関数が通る、という抜け穴は無い。
**機械検査**: `tests/unit/test_db_destructive_guard.py::test_protected_database_is_refused_before_any_connection`
/ `tests/unit/test_db_destructive_guard.py::test_a_url_that_does_not_fix_the_target_is_refused`
/ `tests/unit/test_db_destructive_guard.py::test_libpq_environment_that_can_redirect_the_connection_is_refused`
/ `tests/unit/test_db_destructive_guard.py::test_only_loopback_names_are_local`
/ `tests/unit/test_db_destructive_guard.py::test_same_database_is_detected_through_url_variations`
/ `tests/unit/test_db_destructive_guard.py::test_libpq_override_in_query_is_refused`
/ `tests/unit/test_db_destructive_guard.py::test_explicit_allowlist_admits_a_non_test_name_but_never_the_app_database`
/ `tests/architecture/test_destructive_schema_ops_guarded.py::test_every_destructive_call_site_is_guarded`
/ `tests/architecture/test_destructive_schema_ops_guarded.py::test_checker_flags_only_the_unguarded_function_in_a_file_with_a_guard`
/ `tests/architecture/test_destructive_schema_ops_guarded.py::test_checker_covers_every_destructive_form`
/ `tests/contract/test_ci_db_isolation.py::test_ci_test_database_urls_are_isolated`

## Q. provider の残高不足（ADR-0057）

### INV-71 残高不足・分類不能の拒否は、入力の拒否でも認証障害でもなく、provider アカウントの状態として扱う（ADR-0057）
fal の 401/402/403 は、本文の文書化された印（`fal_refusal.BALANCE_BODY_MARKERS`、出所つき・1表だけ）が
あるときだけ `BALANCE_EXHAUSTED`、印が無い 401 は `AUTH_INVALID`、印が無い 402/403 は `UNKNOWN`。
**HTTP status だけで残高不足と決めない**（公式文書に残高の構造化コードは無い）。
`ProviderBalanceExhaustedError` / `ProviderRefusalUnknownError` は `ProviderUnavailableError`
（ADR-0030 の認証 incident）の子でも `ProviderRejectedError`（ADR-0034 の恒久ブロック）の子でもない。
応答本文の要約を残す（secret・署名付き URL・query を除く。300 文字まで）。
**機械検査**: `tests/unit/test_fal_refusal.py::test_status_alone_never_decides_balance`
/ `tests/unit/test_fal_refusal.py::test_three_statuses_map_to_three_different_kinds`
/ `tests/unit/test_fal_refusal.py::test_markers_live_in_one_place_with_provenance`
/ `tests/unit/test_fal_refusal.py::test_summary_redacts_secrets_urls_and_truncates`
/ `tests/unit/test_fal_refusal.py::test_submit_refusals_use_distinct_error_types`

### INV-72 breaker は provider アカウント単位で永続し、時間では閉じず、予約の前に止める（ADR-0057）
breaker は PostgreSQL（`provider_breakers`）に持つ。鍵は `ProviderAccount.key`。`open` は残高不足・分類不能の
拒否の最初の1件で開き、冪等かつ並行安全（一意制約と条件付き UPDATE。同時に開いても行は1つ・勝者は1人）。
**時間経過では閉じない**。`half_open` / `closed` への遷移は所有者承認つきの操作だけ（`half_open` で
provider が submit を受理したときの `closed` を除く）。migration 0018 は expand-only（新しい表だけ。既存の
CHECK を広げない）。
**機械検査**: `tests/unit/test_provider_health_repo.py::test_breaker_never_closes_with_time`
/ `tests/unit/test_provider_health_repo.py::test_open_is_idempotent_and_keeps_the_first_reason`
/ `tests/unit/test_provider_health_repo.py::test_half_open_needs_open_then_accepted_submit_closes_it`
/ `tests/integration/test_provider_breaker_postgres.py::test_simultaneous_opens_leave_one_row_and_exactly_one_winner`
/ `tests/contract/test_provider_health_migration.py::test_release_preflight_scanner_finds_nothing_in_0018`
/ `tests/contract/test_provider_health_migration.py::test_0018_upgrade_only_creates_tables_and_indexes`
/ `tests/unit/test_paid_job_balance.py::test_open_breaker_stops_before_any_reservation_for_every_fal_provider`
/ `tests/unit/test_paid_job_balance.py::test_open_breaker_does_not_block_awaiting_an_already_accepted_job`
/ `tests/unit/test_paid_job_balance.py::test_non_fal_providers_are_not_gated`
/ `tests/unit/test_paid_job_balance.py::test_accepted_submit_in_half_open_closes_the_breaker`

### INV-73 保留した Episode の再開は、所有者の承認・未照合予約なし・予算内・同時1件の下で、既存の再開経路だけを通る（ADR-0057）
残高不足で止まった Episode は `blocked` のまま `provider_credit_holds` に理由を持つ（新しい EpisodeStatus は無い。
workflow は終わる）。再開（`scripts/provider-credits.py`）は、breaker が `open` のとき・未照合の予約
（`reserved` で evidence なし。dispatched で provider 参照があるもの＝本番の `591d1a8b…` と同じ状態を含む）が
あるとき・当月の見積支出 + 追加見積が `fal_monthly_budget_usd` を超える（または未設定）とき・対象が
`RESUME_MAX_CONCURRENCY` を超える（実行中の再開済みも数える）ときは**拒否**し、何も書かない。
`half-open` / `close` / `resume` は構造化された所有者承認 token（`OWNER-<yyyymmdd>-<id>`。指紋だけ保存）と
入金確認の根拠（attestation）が必須で、既定は dry-run。再開は既存の `POST /episodes/{id}/resume` だけを呼び、
新しい workflow を作らない。成功済みシーンは再利用ゲートで再利用する（再送・再生成しない）。
`POST /episodes/{id}/resume` は、その Episode に**有効な未解放の保留**がある間は 409 で拒否する（保留の解放は上記の
検査をすべて通す CLI だけ。API 経由で承認・予約照合・予算を迂回して有料呼び出しを再開させない）。保留が「有効」とは
未解放かつ Episode が `HELD_EPISODE_STATUSES`（`blocked` / `storyboard_ready`）にあること。完了・取消などで
その状態を離れた Episode の古い行は `open_hold_count` / `list_open_holds` / `held_episode_ids_oldest_first` に
数えない（日次が HELD_BACKLOG のまま止まらない。行は監査のため消さない。review-E P1-1）。
**機械検査**: `tests/unit/test_provider_credits_script.py::test_resume_is_refused_for_the_production_unreconciled_reservation_state`
/ `tests/unit/test_provider_credits_script.py::test_resume_is_refused_while_the_breaker_is_open`
/ `tests/unit/test_provider_credits_script.py::test_resume_is_refused_over_budget_or_without_a_cap`
/ `tests/unit/test_provider_credits_script.py::test_resume_concurrency_is_capped`
/ `tests/unit/test_provider_credits_script.py::test_a_resumed_episode_still_running_counts_toward_the_cap`
/ `tests/unit/test_provider_credits_script.py::test_half_open_needs_a_structured_approval_and_evidence`
/ `tests/unit/test_provider_credits_script.py::test_api_resume_client_calls_only_the_existing_resume_endpoint`
/ `tests/unit/test_provider_credits_script.py::test_succeeded_scenes_are_reused_after_resume_not_regenerated`
/ `tests/unit/test_provider_credits_script.py::test_script_has_no_network_or_provider_imports`
/ `tests/unit/test_provider_credits_script.py::test_a_daily_gate_hold_at_storyboard_ready_resumes_from_production`
/ `tests/unit/test_production_balance_workflow.py::test_a_seven_day_hold_costs_no_more_activity_and_no_more_history`
/ `tests/unit/test_production_balance_workflow.py::test_balance_refusal_ends_the_workflow_blocked_without_a_retry_storm`
/ `tests/unit/test_provider_health_repo.py::test_breaker_and_holds_survive_a_process_restart`
/ `tests/unit/test_hold_lifecycle.py::test_only_episodes_still_in_a_held_state_count`
/ `tests/unit/test_hold_lifecycle.py::test_api_resume_refuses_while_a_credit_hold_is_open`
/ `tests/integration/test_provider_breaker_postgres.py::test_concurrent_episodes_hitting_balance_exhaustion_open_one_breaker_and_double_nothing`

### INV-74 受理されなかったことが確実な拒否は費用 0 で、曖昧な送信は未照合のまま再送しない（ADR-0057）
残高ロック・分類不能の拒否（`ProviderRefusalError`）で終わった予約は `spent` +
`reconciled_by="not_accepted"` で確定し、費用は 0 として数える（`domain/provider/cost.py` の
`not_charged`）。`input_rejected_by_provider` は立てず、入金後は同じ入力を新ラウンドで送れる
（ADR-0034 の恒久ブロックにしない）。費用の区分は `estimated` / `provider_accepted` /
`confirmed_charged` / `not_charged` / `assumed_charged` / `unknown` を台帳の行から導く。送信後の切断・5xx などの
曖昧な送信は `reserved` + dispatched のまま `unknown`（見積額を安全側に数える）で残り、自動で再送しない（INV-15 / INV-67）。
**機械検査**: `tests/unit/test_provider_cost_state.py::test_not_accepted_is_not_charged_and_counts_zero`
/ `tests/unit/test_provider_cost_state.py::test_the_five_states_are_distinguished`
/ `tests/unit/test_paid_job_balance.py::test_balance_refusal_is_not_accepted_zero_cost_and_opens_the_breaker`
/ `tests/unit/test_paid_job_balance.py::test_after_the_top_up_the_same_input_is_submitted_again`
/ `tests/unit/test_paid_job_balance.py::test_ambiguous_submit_stays_unreconciled_and_is_never_resent`
/ `tests/unit/test_paid_job_balance.py::test_not_accepted_refusal_adds_nothing_to_the_recovery_cost`
/ `tests/unit/test_production_metrics_script.py::test_not_accepted_refusals_do_not_count_as_cost`

## R. 日次の事前診断・保留（ADR-0058）

### INV-75 設定された Codex model が必ず失敗する構成は、日次が Codex を呼ぶ前に・prompt を送らずに検知する（ADR-0058）
実効 model（`CODEX_MODEL` > config.toml）が、アカウントの model catalog（`models_cache.json`）に無い・
許可リスト（`CODEX_ALLOWED_MODELS`）に無い・固定が必須（`CODEX_REQUIRE_PINNED_MODEL`）なのに未固定のとき、
`DailyEpisodeWorkflow` は Planner / Codex を呼ばずに non-retryable で失敗し、原因（verdict と model 名）を
失敗に残す。検査は設定ファイルの読み取りだけで利用枠を使わない。**catalog に載っていることは「使える」の
証拠にしない**（`OK` は許可リストにもある場合だけ。許可リストが空なら `UNVERIFIED`）。読めない・古い
catalog は「無い」の根拠にしない。3 つの Codex worker は model 設定を compose の `x-codex-env` 1 箇所から読む。
**機械検査**: `tests/unit/test_codex_model_preflight.py::test_model_missing_from_the_catalog_blocks_before_any_prompt`
/ `tests/unit/test_codex_model_preflight.py::test_a_catalogued_model_is_never_reported_as_verified_without_an_allowlist`
/ `tests/unit/test_codex_model_preflight.py::test_a_model_outside_the_allowlist_blocks`
/ `tests/unit/test_codex_model_preflight.py::test_unreadable_or_stale_catalog_never_blocks_and_never_verifies`
/ `tests/unit/test_codex_model_preflight.py::test_run_preflight_reads_files_only`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_a_blocking_codex_preflight_fails_the_daily_run_before_any_codex_call`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_an_unverified_codex_model_does_not_stop_the_run`
/ `tests/contract/test_codex_env_single_source.py`

### INV-76 provider の breaker が閉じていない間、日次は新しい有料生成を始めず、保留を記録する（ADR-0058）
`DailyEpisodeWorkflow` は Schedule が発火したら必ず走り、入口ゲート（読み取り専用）でモードを決める。
breaker が `open` / `half_open` なら企画・台本・絵コンテ（Codex、無課金）は進めるが、production の前で
`CreditHoldWriter` に保留（`resumable_stage="production"`、Episode 全体）を記録して止まり、有料の
provider を呼ばない。理由は残高不足と確定できたときだけ `WAITING_FOR_CREDITS`、それ以外は
`PROVIDER_REFUSAL_UNKNOWN`。未解放の保留が `MAX_HELD_EPISODES` に達したら新しい Episode を作らない
（Planner も Codex も呼ばない）。したがって breaker が開いたまま 7 日経っても Episode は高々
`MAX_HELD_EPISODES`、有料呼び出しは 0。所有者承認の再開（API / 統一再開が起動する pipeline）は
このゲートに止められない（`credit_gate` は日次が起動したものだけ）。
**機械検査**: `tests/unit/test_daily_credit_hold_workflows.py::test_open_breaker_keeps_planning_and_script_but_starts_no_paid_work`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_an_unconfirmed_refusal_is_not_called_waiting_for_credits`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_half_open_breaker_is_not_used_by_the_daily_run`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_backlog_at_the_cap_creates_no_episode_and_calls_no_codex`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_seven_days_of_open_breaker_are_bounded_and_repeat_no_paid_attempt`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_a_pipeline_not_started_by_daily_is_never_stopped_by_the_gate`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_open_breaker_without_a_hold_writer_refuses_instead_of_going_on_to_paid_work`
/ `tests/unit/test_daily_gate_domain.py`

### INV-77 保留は失敗でも成功でもない。watchdog は失敗・保留・未納品を区別し、保留を見えなくしない（ADR-0058）
保留で止まった pipeline は `PipelineOutcome.HELD`、保留で Episode を作らなかった日次は
`DailyOutcome.HELD_BACKLOG`。どちらも `COMPLETED` / `STARTED`（成功）ではなく、`STOPPED`（失敗）でもない。
watchdog は猶予内（`DEFAULT_HOLD_ESCALATION_DAYS`）の保留を停滞・完成期限の通報から除くが、毎時の
`WatchdogResult.held_*` と `HELD_EPISODES` ログに出す。猶予を超えたら**既存の** `EPISODE_STAGE_STALLED`
を `detail.hold` 付きで記録し（新しい kind・CHECK の拡張をしない）、解放されれば閉じる。保留と無関係の
本物の失敗は保留があっても通報され、保留を読めないときは何も除かない。保留で Episode を作らなかった
日は日次失敗（ADR-0054）にならない。
**機械検査**: `tests/unit/test_hold_watchdog_classification.py`（全テスト）
/ `tests/unit/test_daily_credit_hold_workflows.py::test_backlog_at_the_cap_creates_no_episode_and_calls_no_codex`
/ `tests/unit/test_daily_gate_domain.py::test_recent_holds_are_exempt_and_long_holds_are_escalated`

### INV-78 日次は過去の Episode や有料の仕事を再開せず、変えた分岐は旧履歴を replay できる（ADR-0058）
日次は常に新しい slot の新しい Episode を `SCRIPT` から始める。未完了・保留の Episode の再開は
再開経路（ADR-0032 / ADR-0057）だけが行い、日次の入口ゲートは `half_open`（所有者承認の枠）も使わない。
`DailyEpisodeWorkflow` の新分岐は `workflow.patched("daily-preflight-0058")`、`EpisodePipelineWorkflow` の
新分岐は入力フラグ（旧履歴は False）で振り分け、`DailyWatchdogWorkflow` は変更しない。ADR-0058 より前に
記録した履歴が replay できること。
**機械検査**: `tests/unit/test_daily_credit_hold_workflows.py::test_seven_days_of_open_breaker_are_bounded_and_repeat_no_paid_attempt`
/ `tests/unit/test_daily_credit_hold_workflows.py::test_histories_recorded_before_adr_0058_replay_without_nondeterminism`
/ `tests/unit/test_pipeline_workflows.py::test_history_from_before_the_topic_planner_replays_without_nondeterminism`

## S. ログ基盤の保護・復旧・監視（ADR-0059）

### INV-79 メモリ逼迫での OpenSearch 停止は一時的な低下では起きず、復旧は guard が止めた OpenSearch 1 コンテナだけを条件が揃ったときだけ行う（ADR-0059）
MemAvailable が停止線（1.5GiB）未満の**連続 5 サンプルかつ 180 秒以上**でだけ OpenSearch / Dashboards を止め
（サンプル間 150 秒超は連続でない）、止めるのは `avp2-logging` のコンテナだけ。停止の理由・時刻・サンプル・復旧条件・
試行は state に残る。自動起動は `--auto-restart` を付けたときだけで、`docker start <その 1 コンテナ>` のみ
（compose・rm・run・recreate を呼ばない）。条件は MemAvailable >= 4096MiB の連続 3 サンプル・avp2 の全コンテナが
healthy・停止から 10 分以上・backoff・24 時間 3 回未満。上限後は止めたまま警報。guard が止めていない停止は起こさない。
`check-pipeline.sh` は何も止めない。
**機械検査**: `tests/unit/test_opensearch_guard.py::test_transient_dip_below_threshold_stops_nothing`
/ `tests/unit/test_opensearch_guard.py::test_oscillating_memory_never_starts_a_stop_start_loop`
/ `tests/unit/test_opensearch_guard.py::test_sustained_pressure_still_stops_opensearch_and_dashboards_only`
/ `tests/unit/test_opensearch_guard.py::test_opensearch_stop_raises_alert_records_state_and_spares_others`
/ `tests/unit/test_opensearch_guard.py::test_auto_restart_is_off_unless_the_flag_is_given`
/ `tests/unit/test_opensearch_guard.py::test_auto_restart_starts_only_the_opensearch_container`
/ `tests/unit/test_opensearch_guard.py::test_restart_requires_all_workers_healthy`
/ `tests/unit/test_opensearch_guard.py::test_never_restarts_an_opensearch_the_guard_did_not_stop`
/ `tests/unit/test_opensearch_guard.py::test_flap_cap_stays_stopped_and_alerts_after_max_restarts`
/ `tests/unit/test_opensearch_guard.py::test_guard_source_never_uses_compose_or_destructive_docker_verbs`
/ `tests/unit/test_opensearch_guard.py::test_guard_constants_match_the_fluent_bit_config_and_documented_conditions`
/ `tests/contract/test_opensearch_guard_deploy.py::test_check_pipeline_no_longer_stops_any_container`
/ `tests/contract/test_opensearch_guard_deploy.py::test_guard_unit_is_stop_only_by_default_auto_restart_is_a_separate_decision`

### INV-80 ログ基盤・ホスト監視の警報と heartbeat は OpenSearch に依存せず、監視が止まったことを別の timer が検知する（ADR-0059）
警報は journal 向けの stderr 行・`alerts.jsonl`・`alerts-current.json` に出る（OpenSearch・Temporal・DB を経由しない）。
guard と host-process-monitor（PID/zombie）は毎回 heartbeat を書き、**別の unit・別の timer** の `monitor-watch.py` が
古い・無い heartbeat を crit にする。無い heartbeat を OK にしない。watcher は読み取り専用。
**機械検査**: `tests/unit/test_avp_alerts.py::test_same_alert_is_not_repeated_until_the_interval_and_resolves_once`
/ `tests/unit/test_opensearch_guard.py::test_alert_is_written_to_stderr_with_a_journal_priority`
/ `tests/unit/test_opensearch_guard.py::test_heartbeat_is_written_every_run_even_when_everything_is_fine`
/ `tests/unit/test_monitor_watch.py::test_a_stopped_guard_is_flagged_while_the_other_monitor_stays_quiet`
/ `tests/unit/test_monitor_watch.py::test_a_missing_heartbeat_is_critical_not_ok`
/ `tests/unit/test_monitor_watch.py::test_host_process_monitor_writes_heartbeat_and_pid_alerts_to_the_shared_path`
/ `tests/unit/test_monitor_watch.py::test_watcher_never_touches_docker_network_or_other_processes`
/ `tests/contract/test_opensearch_guard_deploy.py::test_watch_is_a_separate_unit_and_timer_from_the_monitors_it_watches`

### INV-81 ログ収集の欠損（buffer 上限・retry 窓の接近・破棄）は黙らず、取り戻せる範囲の調査は読み取り専用（ADR-0059）
Fluent Bit の filesystem buffer が上限の 70% / 90%、最古の chunk が retry 窓（retry_limit x 平均 backoff）の 50% / 80% に
達したら破棄の**前**に、chunk の破棄（`cannot be retried`）は**後**に警報になる。停止窓のログの recoverable /
unrecoverable は `log-recoverability.py` が出し、元の json-file には書かない。
**機械検査**: `tests/unit/test_opensearch_guard.py::test_buffer_near_its_limit_alerts_before_any_drop`
/ `tests/unit/test_opensearch_guard.py::test_oldest_chunk_nearing_the_retry_window_alerts_before_drops`
/ `tests/unit/test_opensearch_guard.py::test_dropped_chunks_in_fluent_bit_log_raise_a_crit_alert`
/ `tests/unit/test_log_recoverability.py::test_rotated_away_start_of_the_window_is_unrecoverable`
/ `tests/unit/test_log_recoverability.py::test_backfill_copies_only_window_lines_verbatim_and_never_writes_the_source`
/ `tests/unit/test_log_recoverability.py::test_source_only_uses_read_only_docker_verbs`

## T. migration の走査（ADR-0060）

### INV-82 CHECK 語彙の拡張だけが release 走査の contract から外れ、その免除は宣言と機械検査で縛られる（ADR-0060）
`migration_compat` は `drop*` を contract として扱い続ける。例外は 1 つだけ: migration が
`CHECK_WIDENING`（table / constraint / column / old / new。リテラル、`old` ⊊ `new`、重複なし）を宣言し、
`upgrade()` が `with op.batch_alter_table(table) as b:` の本体にちょうど
`b.drop_constraint(constraint, type_="check")` → `b.create_check_constraint(constraint, "column IN ('..', ..)")`
だけを持ち（`batch_alter_table` にキーワード引数〔recreate / copy_from / table_args / reflect_args / schema〕が
あれば免除しない。`constraint` は CHECK の命名規約 `ck_*` で、`*_pkey` / `*_fkey` / `*_key` / `uq_*` / `fk_*` /
`pk_*` など PK/FK/UNIQUE の名前は宣言不正として BLOCKED。2026-10-25 追記: 宣言を信用せず名前と引数で絞る）、IN の集合が `new` と一致するとき、その 1 組だけが contract と数えられない（免除は preflight の
detail に残る）。宣言の不正・不一致・別名・別の制約種別・別の表・順序違い・with の外・2 回目の drop・
同じファイルの他の drop / 破壊的 SQL は BLOCKED のまま。宣言の `old` が実際の旧制約と一致することと、
旧コードの値が拡張後も通ることは、prod 形から流す実 INSERT のテストが示す。
**機械検査**: `tests/unit/test_release_check_widening.py`（宣言どおりは PASS、否定例は全て BLOCKED、
実 0017 の認識）
/ `tests/contract/test_migration_0017_check_widening.py`（old が 0016 の実制約と一致・旧値は拡張後も通る・
新値が通る・未知値は拒否・downgrade で新値の行だけ消えて旧制約に戻る・`new` = `AnomalyKind`）
/ `tests/integration/test_migration_against_postgres.py::test_0017_widens_the_kind_check_and_keeps_old_code_working_on_postgres`
/ `tests/unit/test_release_migration_scan.py`（既存の bypass 例は従来どおり BLOCKED）

## U. 破壊的操作の接続先検証（ADR-0055）

### INV-83 破壊的な DB 操作は、実際に繋がっている接続が印のある `_test` DB であると確認できたときだけ実行される（ADR-0055）
URL は `connect_args` / `creator` / 環境変数で実際の接続先と食い違いうる。破壊的な文（`DROP TABLE|SCHEMA|DATABASE` /
`TRUNCATE`）は、PostgreSQL では実行直前に `current_database()` と DB コメントの印（`avp-test-database`）を
読み、DB 名が `_test` で終わらない・URL の DB と違う・印が無いなら `UnsafeDatabaseError`（DDL は走らない）。
`DROP DATABASE` は消す対象の名前も `_test` で終わること。
**文の判定**（2026-10-25 追記、独立レビュー E の P2-4）: `;` の分割はコメント（`--`・入れ子の `/* */`）と引用
（`'..'` `E'..'` `".."` `$tag$..$tag$`）を正しく読んだ上で行う（`/* ; */ DROP TABLE x` が漏れない）。
`DO` ブロックで `EXECUTE` または `DROP`/`TRUNCATE` の語を含むものは動的 SQL として破壊的扱い（安全側）。
`ALTER TABLE .. DROP [COLUMN] c` は破壊的（列のデータが消える）、`DROP CONSTRAINT` / `DROP DEFAULT|NOT NULL` は
対象外（データを消さず、CHECK 作り直しの migration が日常的に行う）。`WHERE` 無しの `DELETE FROM` は実行時の
守衛（conftest）だけが破壊的扱いにする（architecture テストが拾うリテラルには数えない）。
範囲外のまま: `CALL`/`SELECT fn()` が中で行う DDL、文字列連結で組み立てた動的 SQL を `DO` 以外から実行する経路、
SQLAlchemy Engine を通らない生 psycopg 接続、サブプロセスの alembic。
`guarded_drop_all` / `guarded_create_all` は同じ検査を明示で行い、
tests の `conftest.py` は全 PostgreSQL 接続の文の入口にも同じ検査を入れる（`drop_all` も生 SQL も止まる）。
本番と `avp_test` が同じインスタンスにあるときは、サーバのアドレスでは区別できないので DB 名と印で区別する
（印の付与は CI の使い捨て DB か所有者の手作業。別インスタンスへの分離が最終形）。
**機械検査**: `tests/integration/test_destructive_db_mistake.py`（失敗注入12: 本番に見立てた DB・`connect_args`・
`creator`・libpq 環境変数・印の無い `_test` DB・`DROP DATABASE` のどれでも拒否され、実 DB の表と行が残る。
印のある DB は通る対照つき）
/ `tests/unit/test_db_destructive_guard.py::test_a_connect_args_style_override_is_stopped_by_the_live_check`
/ `tests/unit/test_db_destructive_guard.py::test_live_check_refuses_everything_else`
/ `tests/unit/test_db_destructive_guard.py::test_destructive_statements_are_verified_before_they_run`
/ `tests/unit/test_db_destructive_guard.py::test_drop_database_names_must_end_with_test_even_on_a_verified_connection`
/ `tests/contract/test_ci_db_isolation.py::test_ci_steps_that_get_a_test_database_also_mark_it`

## V. fal のローカル予算（ADR-0061）

### INV-84 fal の月間予算が 0 / 未設定のとき、新しい有料 submit は 0 回で、日次は失敗せず正常終了する（ADR-0061）
`FAL_MONTHLY_BUDGET_USD` が未設定・不正・0 のとき、`PaidJobRunner.submit` は予約も prepare も submit もせず
`ProviderBudgetHeldError`（`needs_input`）で止め、日次の入口は `PAID_HELD`、production の前は保留を記録して
`PipelineOutcome.HELD` で終える。**未設定は無制限ではない**。保留は失敗でも成功でもなく（INV-77）、上限
`MAX_HELD_EPISODES` に達すれば 3 日目以降は新しい Episode を作らず `HELD_BACKLOG`（INV-76）。予算ゲートは
再利用・受理済み・未照合の早期 return の後ろなので、完成済みシーンは再生成されず、未照合予約は予算が正に
なっても再送されない（INV-17 / INV-67）。予算を足したあとの再開は所有者承認つき `resume` だけ（日次は
再開しない / INV-78）。保留の行は `reason=waiting_for_credits` に `snapshot.hold_detail`（`budget_status` /
`decision_reason`）を持つ。有料の入口（`PaidJobRunner` を組む worker・pipeline worker）が予算を結線している
ことは契約テストが検査する。
**機械検査**: `tests/unit/test_budget_gate_domain.py` / `tests/unit/test_paid_job_budget.py`
/ `tests/unit/test_budget_zero_daily_workflows.py::test_a_month_of_zero_budget_never_fails_and_is_bounded`
/ `tests/unit/test_budget_zero_daily_workflows.py::test_zero_or_unset_budget_plans_scripts_then_holds_and_ends_normally`
/ `tests/contract/test_fal_budget_wiring.py` / `tests/contract/test_compose_workers.py::test_per_worker_minimal_env`
/ `tests/unit/test_hold_watchdog_classification.py::test_budget_holds_are_held_not_failed_and_stay_visible`

## W. release の未照合予約 waiver（ADR-0062）

### INV-85 未照合予約の waiver は release 限定・1 件限り・期限つきで、台帳を書かず、`provider_reservations` 以外を免除しない（ADR-0062）
`provider_reservations` は、`reserved` がちょうど 1 件で、その id・provider request id・行の指紋と、SHA・
所有者承認参照・72 時間以内の期限・証跡の sha256・予算設定が明示的な 0・実行中 workflow 0 が全て揃う
waiver があるときだけ `WAIVED` になる。1 つでも欠ける・読めない・件数が読み取り中に変わる・別の `reserved`
行がある場合は BLOCKED。waiver が無ければ従来どおり BLOCKED（detail も同じ）、件数 0 なら waiver は無視。
`WAIVED` は `WAIVABLE_CHECKS` でだけ必須を満たし、他のチェックでは総合 BLOCKED。予約は `reserved` のまま
で台帳・DB は書かない（再送禁止は維持）。出力に所有者承認の生 token は出さない（指紋のみ）。
**機械検査**: `tests/unit/test_release_reservation_waiver.py::test_single_target_reservation_with_all_conditions_is_waived_and_ready`
/ `tests/unit/test_release_reservation_waiver.py::test_explicit_zero_budget_is_accepted`
/ `tests/unit/test_release_reservation_waiver.py::test_count_zero_ignores_the_waiver`
/ `tests/unit/test_release_reservation_waiver.py::test_no_waiver_file_keeps_the_original_blocked_detail`
/ `tests/unit/test_release_reservation_waiver.py::test_a_second_reserved_row_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_count_changing_during_the_read_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_expired_waiver_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_window_over_72_hours_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_expiry_not_after_approval_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_approved_in_the_future_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_naive_times_block`
/ `tests/unit/test_release_reservation_waiver.py::test_request_id_mismatch_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_reservation_id_mismatch_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_wrong_sha_and_wrong_kind_block`
/ `tests/unit/test_release_reservation_waiver.py::test_unparseable_waiver_is_blocked_not_unknown`
/ `tests/unit/test_release_reservation_waiver.py::test_fingerprint_mismatch_blocks_when_the_row_changed`
/ `tests/unit/test_release_reservation_waiver.py::test_row_that_is_not_reserved_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_only_an_explicit_zero_budget_is_accepted`
/ `tests/unit/test_release_reservation_waiver.py::test_waiver_must_require_zero_budget`
/ `tests/unit/test_release_reservation_waiver.py::test_missing_approver_bad_token_or_reason_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_running_workflows_block`
/ `tests/unit/test_release_budget_setting_source.py::test_process_env_that_differs_from_dotenv_is_unavailable`
/ `tests/unit/test_release_reservation_waiver_safety.py::test_a_valid_waiver_does_not_relax_any_other_check`
/ `tests/unit/test_release_reservation_waiver_safety.py::test_a_waived_run_calls_only_protocol_read_methods`
/ `tests/unit/test_release_reservation_waiver_safety.py::test_the_resubmit_guards_that_the_waiver_relies_on_still_exist`
/ `tests/unit/test_release_reservation_waiver_safety.py::test_waiver_is_ignored_in_post_deploy_mode`
/ `tests/unit/test_release_reservation_waiver_safety.py::test_a_waiver_bound_to_sha_a_does_not_apply_to_sha_b`
/ `tests/unit/test_release_reservation_waiver.py::test_evidence_hash_mismatch_or_missing_blocks`
/ `tests/unit/test_release_reservation_waiver.py::test_collector_failure_is_unknown`
/ `tests/unit/test_release_reservation_waiver.py::test_waived_on_a_non_waivable_check_is_blocked`
/ `tests/unit/test_release_reservation_waiver.py::test_waived_counts_as_satisfied_only_for_reservations`
/ `tests/unit/test_release_reservation_waiver.py::test_approved_report_accepts_waived_reservations_only`
/ `tests/unit/test_release_reservation_waiver.py::test_other_checks_are_identical_with_and_without_the_waiver_file`
/ `tests/unit/test_release_reservation_waiver.py::test_text_and_json_show_waived_with_a_warning`
/ `tests/unit/test_release_reservation_waiver.py::test_default_waiver_path_and_fingerprint_are_stable`
/ `tests/architecture/test_release_preflight_read_only.py`

## X. fal の月間ハードキャップ（ADR-0063）

### INV-86 fal 族の新しい有料予約は、予約と同じトランザクションで `month_counted + estimate <= FAL_MONTHLY_BUDGET_USD` を満たすときだけ作られる（ADR-0063）
`FAL_MONTHLY_BUDGET_USD`（正の値）は fal 族（`PROVIDER_FAMILY` の `FAL_IMAGE` / `FAL_VIDEO`）の**当月（UTC）合計の上限**。
`ProviderReservationRepository.reserve` が INSERT と同じ DB トランザクションで `pg_advisory_xact_lock`
（`sha256("fal-monthly-cap|YYYY-MM")` の 64bit。**月ロック → 入力ロック**の固定順序）を取り、SQL の `SUM` で
`month_counted`（`reserved_at >= UTC 月初` の全行の見積。除くのは費用区分 `NOT_CHARGED` の行だけ。曖昧・未 dispatch の
`reserved`・課金前提は見積額で数える）を読み、純粋関数 `evaluate_cap` で判定する。ちょうど等しいなら許可、超えれば
`ProviderMonthlyBudgetExceededError`（予約行なし・fal 呼び出し 0・クラッシュではなく `WAITING_FOR_CREDITS` の保留で
`decision_reason=monthly_cap_exceeded`。再開は承認つき `resume` だけ）。見積が出せない・数える行に見積 NULL があるときは
`unknown_cost` で拒否（fail closed）。負・非有限の入力も拒否。同じ `idempotency_key` の既存予約（再試行・replay・再起動）は
二重に数えず、上限でも拒否しない（検査は本当に新しい行にだけ効く）。月初は `month_start_utc` の 1 関数で、
`provider-credits.py` の status / resume も同じ `month_counted` を使う。`reserve` の本番の fal 呼び出し元は
`paid_job.py` の 1 箇所で、`PaidJobRunner` は ENABLED のとき必ず上限を渡す（契約テストが固定）。予算を 0 にしても進行中の
予約は取り消さず、新規だけ止まる。上限の変更には worker の再作成が要る（ADR-0061）。上限は全ブランド・全 Episode で 1 つ。
**機械検査**: `tests/unit/test_budget_cap_domain.py` / `tests/unit/test_monthly_cap_ledger.py`
/ `tests/unit/test_paid_job_monthly_cap.py` / `tests/contract/test_fal_monthly_cap_wiring.py`
/ `tests/integration/test_monthly_cap_concurrency.py::test_concurrent_reservations_never_exceed_the_cap`
/ `tests/integration/test_monthly_cap_concurrency.py::test_mixed_input_and_month_locks_do_not_deadlock`
## Y. 投稿の説明欄（ADR-0070）

### INV-90 視聴者向け description に内部 ID を出さない
`AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER` が ON のとき、description に `avpu[0-9a-f]{24}`・UUID・MinIO の key / URL を
含めない。lint の定義は `contracts/upload.py::INTERNAL_ID_PATTERNS` の1つで、組み立て（`build_youtube_metadata`）と
`videos.insert` の body（`infrastructure/youtube/metadata.py::insert_body`）の双方が通す。flag は既定 OFF で、OFF の
挙動は従来と同じ（description の最終行に marker。ADR-0070）。
**機械検査**: `tests/contract/test_upload_metadata_lint.py::test_description_never_contains_internal_ids`
/ `tests/contract/test_upload_metadata_lint.py::test_insert_body_description_has_no_marker`
/ `tests/contract/test_upload_metadata_lint.py::test_insert_body_lints_the_final_body`
/ `tests/contract/test_upload_metadata_lint.py::test_lint_catches_obfuscated_and_adjacent_internal_ids`
/ `tests/contract/test_upload_metadata_lint.py::test_lint_has_no_false_positive_on_normal_text`
/ `tests/contract/test_upload_metadata_lint.py::test_flag_off_is_byte_identical_to_the_legacy_shape`
/ `tests/contract/test_upload_metadata_lint.py::test_insert_body_is_unchanged_with_flag_off`

### INV-91 投稿マーカーは tags に残り、旧 description のマーカーも照合できる
description から marker を外しても `tags` は `upload_marker(upload_key)` を持つ。`find_video_by_marker` は tags のみの
動画と、description にだけ marker を持つ旧動画の両方に一致する（過去動画は変更・削除・再投稿しない）。
description の検査とは別のテストで固定する。実 YouTube が tags のみの動画を返すことの確認は未了で、
本番で flag を ON にする前提条件（ADR-0070）。
**機械検査**: `tests/contract/test_upload_metadata_lint.py::test_tags_keep_the_reconciliation_marker`
/ `tests/contract/test_upload_metadata_lint.py::test_lookup_matches_tags_only_and_legacy_description_only_videos`
/ `tests/unit/test_upload_hardening.py::test_fake_marker_lookup_matches_description_lines`

### INV-92 必須部分（クレジット・出典）を黙って切り詰めない
description の 5000 UTF-8 bytes は必須部分を先に確保し、残りだけを本文に割り当てる。必須部分だけで超えるときは
切り詰めずに `DescriptionRequiredPartsTooLongError` を上げる（Upload 工程で `needs_input` に写す配線は別タスク）。
**機械検査**: `tests/contract/test_upload_metadata_lint.py::test_required_parts_over_the_limit_raise_instead_of_truncating`
/ `tests/contract/test_upload_metadata_lint.py::test_required_parts_survive_a_long_body`
/ `tests/contract/test_upload_metadata_lint.py::test_required_parts_exactly_filling_the_budget_are_kept`

## Z. 地図・地理（ADR-0071）

### INV-93 実在の位置を主張する地図シーンは GeoManifest を持ち、無ければ通らない
地図が実在の場所を表すと主張するシーンは `GeoManifest` を要する。manifest 無しは BLOCK（fail-closed）。
模式図（schematic）を実在の位置として提示することも BLOCK。
**機械検査**: `tests/unit/test_geo_verify.py::test_a_scene_claiming_a_real_location_without_a_manifest_is_blocked`
/ `tests/unit/test_geo_verify.py::test_a_schematic_cannot_back_a_real_location_claim`
（純関数 `verify_scene` の段階まで。距離の許容は自己申告で広げられない:
`tests/unit/test_geo_verify.py::test_a_declared_uncertainty_cannot_launder_a_place_900_km_off`
/ `tests/unit/test_geo_verify.py::test_the_tolerance_never_exceeds_the_ceiling_of_its_feature_type`。
claim の era / region の一致、資料が位置を支持するかは未検査。**どの workflow・render もまだ検証を呼ばない**ので、「全ての地図シーンが
検証を通る」ことは未検査。接続する枝で検査する）

### INV-94 地図の検証で BLOCK / REVIEW になったものは、LLM の評価で PASS にならない
`GeoVerification.verdict` は check の最悪値と一致しなければ作れず、LLM の評価は `combine_verdicts` を
通してのみ合流し、等しいか厳しい側にしか動かさない。
**機械検査**: `tests/contract/test_geo_contracts.py::test_a_verification_cannot_be_milder_than_its_worst_check`
/ `tests/unit/test_geo_combine.py::test_a_critic_pass_cannot_override_a_deterministic_block`
/ `tests/unit/test_geo_combine.py::test_a_critic_pass_cannot_override_a_deterministic_review`
/ `tests/unit/test_geo_combine.py::test_the_combined_verdict_is_never_milder_than_either_input`
/ `tests/unit/test_geo_combine.py::test_a_score_is_not_accepted_in_place_of_a_verdict`
（`combine_verdicts` を通る経路まで。実際の QualityGate がこの関数以外で最終 verdict を作らないことは、
接続する枝で検査する。未接続の間は未検査）

### INV-95 座標は名前付きの lat / lon（EPSG:4326）で持ち、範囲を検証する
`GeoPlace` / `GeoBBox` / `ReferencePlace` は `lat` / `lon` を名前で持ち、範囲外・NaN・無限大・CRS 違い・
位置引数の座標ペアを受け取らない。
**機械検査**: `tests/contract/test_geo_contracts.py::test_coordinates_outside_epsg4326_ranges_are_rejected`
/ `tests/contract/test_geo_contracts.py::test_coordinates_are_named_fields_not_a_positional_pair`
/ `tests/contract/test_geo_contracts.py::test_only_epsg4326_is_accepted`
/ `tests/contract/test_geo_contracts.py::test_coordinates_reject_bool_and_string_coercion`
/ `tests/contract/test_geo_contracts.py::test_bbox_is_named_ordered_and_in_range`

### INV-96 模式図は「縮尺不同（Not to scale）」の表示を持ち、無ければ BLOCK
`map_kind == schematic` の manifest は `schematic_disclaimer` に表示文を持つ。無い・空・縮尺不同を示す語が
無いものは BLOCK。
**機械検査**: `tests/unit/test_geo_verify.py::test_a_schematic_without_the_disclaimer_is_blocked`
/ `tests/unit/test_geo_verify.py::test_a_schematic_with_the_disclaimer_passes`
/ `tests/unit/test_geo_verify.py::test_a_schematic_that_claims_accuracy_or_omits_the_phrase_is_blocked`
/ `tests/unit/test_geo_verify.py::test_accepted_disclaimers_are_normalized`
/ `tests/contract/test_geo_contracts.py::test_the_disclaimer_vocabulary_is_defined_once_in_contracts`
（受け付けるのは縮尺が正確でないと述べる句だけ。manifest の検査まで。動画フレームに実際に表示されて
いるかは未検査 — 描画後の照合は後続）

## AA. BGM（ADR-0072。契約と純粋ドメインのみ。Render・説明欄・DB への結線は後続）

番号 INV-41〜89 は他の枝が予約している（`docs/architecture/v3-phase0-audit.md`）。この節は INV-97〜100。

### INV-97 選曲できるのは、承認済み・期限内・プラットフォーム/形式が許可された曲だけ
候補になる曲は `license_status=approved`・`status=active`・期限内（`valid_until` が現在より後）・
再利用/商用/改変が許可・対象プラットフォームと形式（shorts/longform）が許可・帰属必須なら指定文あり、の全てを満たす。
未確認・失効・取消・Shorts 不可・指定文なしの曲は、他に曲が無くても選ばれず、除外理由が機械可読で返る。
直近の使用（連投抑制）・mood・尺は並び順にだけ効き、合法な曲を除外せず、違法な曲を押し上げない。
ライセンスの事実そのもの（承認可否）はここで決めない。人が証拠を見て決める。
**機械検査**: `tests/unit/test_music_selection.py::test_only_legal_tracks_are_candidates_for_shorts`
/ `tests/unit/test_music_selection.py::test_each_license_fact_excludes_the_only_track`
/ `tests/unit/test_music_selection.py::test_valid_until_boundary_is_exclusive_of_now`
/ `tests/unit/test_music_selection.py::test_recent_use_never_excludes_the_only_legal_track`
/ `tests/unit/test_music_selection.py::test_recent_use_never_promotes_an_illegal_track`
/ `tests/unit/test_music_contracts.py::test_approved_requires_evidence_and_check_time`
（選曲を実際に呼ぶ Render / AudioDirector は未結線のため、**結線後に「承認済み以外が Render へ渡らない」ことは未検査**）

### INV-98 帰属必須の指定文は、全文を説明欄に置くか、置けないなら Episode を止める
帰属必須の曲を使う cue sheet は、カタログの `exact_attribution_text`（原文）をそのまま取り出す。
cue 側のコピーが原文と違う・欠ける・曲がカタログに無い・source sha / license_evidence_hash が違う・選曲後に取消/失効/期限切れ/隔離になった場合はエラーにして推測で補わない。
5000 UTF-8 bytes の説明欄に収まらないときは BLOCK を返し、切り詰めた版は作らない。
**機械検査**: `tests/unit/test_music_credits.py::test_credit_required_track_yields_exact_text`
/ `tests/unit/test_music_credits.py::test_stale_or_altered_credit_copy_in_cue_is_an_error_not_silently_replaced`
/ `tests/unit/test_music_credits.py::test_fit_credits_blocks_instead_of_truncating`
/ `tests/unit/test_music_credits.py::test_fit_credits_counts_utf8_bytes_not_characters`
/ `tests/unit/test_music_contracts.py::test_attribution_text_is_required_iff_attribution_required`
/ `tests/unit/test_music_contracts.py::test_attribution_text_with_chars_the_description_sanitizer_rewrites_is_rejected`
/ `tests/unit/test_music_contracts.py::test_attribution_text_blank_after_removing_whitespace_and_format_chars_is_rejected`
/ `tests/unit/test_music_credits.py::test_fitted_credits_are_byte_identical_to_what_is_sent`
/ `tests/unit/test_music_credits.py::test_cue_license_evidence_hash_must_match_the_catalog`
/ `tests/unit/test_music_credits.py::test_track_that_stopped_being_usable_after_selection_is_rejected`
（収まるかの判定は、送信時と同じ `sanitize_youtube_text`（contracts/upload）の後のバイト数で行う。指定文に `<` `>` は持てない。
説明欄の組み立てと投稿前 Gate への結線は後続。**投稿が実際に BLOCK されることは未検査**）

### INV-99 BGM の取込は管理者の明示操作であり、agent・日次の通常制作は行わない
カタログへの取込は、人が音源と証拠を用意して明示的に行う。取込検査は実ファイルの sha256 と申告の一致、
object_key が `music/originals/<sha>(.<拡張子>)?` に完全一致すること、同一 sha の再利用（再収集なし）、同一 track_id の
中身違い（変造）の拒否を行う。既存曲のライセンス事実は再取込の申告で変わらない。
agent・日次制作は音源を取得せず、未収録なら収集依頼を積むだけ（設計 v3 §4.1）。
**機械検査**: `tests/unit/test_music_ingest.py::test_sha_mismatch_is_rejected_as_tampered`
/ `tests/unit/test_music_ingest.py::test_same_file_is_deduplicated_to_the_existing_track`
/ `tests/unit/test_music_ingest.py::test_duplicate_never_upgrades_the_existing_license_facts`
/ `tests/unit/test_music_ingest.py::test_same_track_id_with_different_bytes_is_rejected`
/ `tests/architecture/test_music_boundaries.py::test_production_and_provider_billing_modules_do_not_import_music`
（production / provider から domain.music を import させないところまでは検査済み。
**取込を呼べる入口を管理者の CLI に限定すること、MinIO の immutable 設定は未検査**。取込の入口はまだ存在しない）

### INV-100 cue sheet は production の成果物から独立しており、BGM だけの変更で fal を呼ばない
`contracts/music.py` は production Artifact の契約（画像・音声・動画・状態語彙）を import せず、cue sheet は
それらの Artifact を参照しない。時間・音量は int で持ち、hash を揺らさない。cue は start_ms 昇順で、同じ scene の cue は重ならない（別 scene どうしの重なり=クロスフェードは許す）。`domain/music` は純粋（I/O・時計なし）。
**機械検査**: `tests/architecture/test_music_boundaries.py::test_music_contract_does_not_depend_on_production_artifacts`
/ `tests/architecture/test_music_boundaries.py::test_domain_music_has_no_io_or_clock_imports`
/ `tests/architecture/test_music_boundaries.py::test_domain_music_does_not_read_the_clock_or_open_files`
/ `tests/architecture/test_music_boundaries.py::test_domain_music_imports_only_contracts_and_itself`
/ `tests/unit/test_music_contracts.py::test_level_is_integer_so_hash_is_stable`
（構造上の独立だけを検査。**BGM のみ差替えで fal の予約が 0 件・Render と QA だけが再実行される（T16）ことは未検査**。
`render_input_hash` に cue sheet をどう入れるかは Render 結線の枝で決める）

## AB. 品質ゲート（ADR-0073。INV-41〜89 は他の枝の予約帯。欠番は意図的）

### INV-101 無い・作れなかった・壊れた・今の final_video / 入力に結び付かない品質レポートは PASS ではない
投稿可否の判定（`domain.quality.evaluate_for_upload`）は fail-closed: レポート無し（`None`）、作成失敗、
取り込めない、schema_version 違い、未知の verdict、検査が空、保存 verdict が検査からの再計算と不一致、
final_video の sha256 または input_hash が**今の**ものと不一致、未来日付は、すべて拒否し理由コードを返す
（鮮度の拒否経路は INV-107）。
許可は検証済みの PASS と WARN（報告つき）だけ。（設計 v3 T22）
**機械検査**: `tests/unit/test_quality_upload_decision.py`（全拒否経路と許可経路）
/ `tests/contract/test_quality_contract.py::test_malformed_reports_are_rejected_with_a_reason`
/ `tests/contract/test_quality_contract.py::test_non_report_input_is_unparsable`
/ `tests/unit/test_quality_upload_decision.py::test_no_input_can_make_evaluate_raise`（総関数・fail-closed）
/ `tests/unit/test_quality_upload_decision.py::test_deeply_nested_json_is_refused_not_raised`
/ `tests/unit/test_quality_upload_decision.py::test_naive_generated_at_is_refused_not_raised`
/ `tests/unit/test_quality_upload_decision.py::test_checks_must_be_a_tuple_of_quality_checks`
/ `tests/contract/test_quality_contract.py::test_error_messages_never_echo_untrusted_values`（値を例外文に写さない）
（判定は純粋関数の範囲。`upload_gate` への配線は INV-104、flag ON のときだけ）

### INV-102 決定論の BLOCK / REVIEW は LLM の判定で緩まない
`domain.quality.aggregate` は決定論の検査だけが BLOCK を作れ、LLM の検査（`deterministic=False`）は
結果を厳しくする方向にだけ働く（LLM の BLOCK は REVIEW 止まり、LLM の PASS は何も緩めない）。
決定論の検査が無いとき、LLM だけでは PASS にならない。（設計 v3 T32）
**機械検査**: `tests/unit/test_quality_aggregate.py::test_llm_pass_cannot_relax_a_deterministic_block`
/ `tests/unit/test_quality_aggregate.py::test_llm_pass_cannot_relax_a_deterministic_review`
/ `tests/unit/test_quality_aggregate.py::test_adding_an_llm_check_never_relaxes_the_result`（全組み合わせの単調性）
/ `tests/unit/test_quality_aggregate.py::test_llm_only_pass_is_still_not_proof_of_pass`
/ `tests/unit/test_quality_upload_decision.py::test_llm_pass_does_not_rescue_a_deterministic_block_at_the_gate`

### INV-103 Agent の tool allowlist は read-only の語彙だけで、課金・投稿・migrate・schedule・予約解放を含まない
許可は `contracts.agent_tool_policy.PolicyToolId` の6つだけ。`fal.generate` / `youtube.upload` / `db.migrate` /
`schedule.pause` / `provider_reservations.release` は禁止集合に1つだけ定義し、許可と交わらない。
`validate_tool_request` は許可外（禁止・似た名前・不正な型）を構造化された拒否で返す。
品質判定・tool 契約のモジュールは I/O・provider・`ProviderReservationRepository`・workers を import しない。（設計 v3 T04）
**機械検査**: `tests/architecture/test_quality_gate_boundaries.py::test_forbidden_tool_ids_are_not_in_the_allowlist`
/ `tests/architecture/test_quality_gate_boundaries.py::test_the_allowlist_detector_catches_a_violating_fixture`
/ `tests/architecture/test_quality_gate_boundaries.py::test_quality_and_agent_tool_modules_do_no_io_and_import_no_provider_or_worker`
/ `tests/architecture/test_quality_gate_boundaries.py::test_the_io_detector_catches_violating_fixtures`
/ `tests/unit/test_agent_tool_policy.py::test_a_web_document_injected_upload_request_is_rejected_and_structured`
/ `tests/unit/test_agent_tool_policy.py::test_forbidden_tools_are_rejected_as_forbidden`
/ `tests/unit/test_agent_tool_policy.py::test_case_whitespace_and_nfkc_variants_of_forbidden_ids_are_forbidden_attempts`
/ `tests/unit/test_agent_tool_policy.py::test_an_unknown_id_is_never_echoed_only_its_length_and_hash`
（tool の実行器・Agent への登録はまだ無い。語彙と判定だけ）

### INV-104 `AVP_QUALITY_GATE_ENFORCE` が ON のとき、`upload_gate` と手動投稿は有効な品質判定が無ければ投稿を拒否する
ADR-0076。設定キーは `contracts.quality.QUALITY_GATE_ENFORCE_ENV`（`AVP_QUALITY_GATE_ENFORCE`。既定 **OFF**）が
唯一の定義で、`Settings.quality_gate_enforce` が読む。**OFF では `_refusal()` / 手動経路の挙動は従来と同一**。
ON のとき `upload_gate._refusal()` は既存の拒否（存在・状態・final_video・予約）をすべて先に評価し、通った場合だけ
決定論の検査（`domain.quality.deterministic.build_deterministic_report`）で `QualityReport` を作って
`evaluate_for_upload`（`current_rule_version=QUALITY_RULE_VERSION`）に渡す。PASS/WARN だけが許可で、REVIEW・BLOCK・
作成失敗・評価例外・鮮度不一致は `quality: <理由コード>` で拒否（PASS に倒さない）。`UploadGateRequest/Result`・
Activity・workflow・PipelineStage・Episode 状態は変えない。手動 `POST /episodes/{id}/upload` は ON のとき BLOCK と
作成失敗を 409、REVIEW は許可して承認を構造化ログに残す。LLM の検査は実装しない（`model=None`）。
**機械検査**: `tests/unit/test_quality_gate_wiring.py::test_on_without_any_deterministic_check_is_refused`
/ `...::test_on_block_is_refused` / `...::test_on_review_is_refused_and_episode_stays_render_ready`
/ `...::test_on_pass_and_warn_are_allowed_when_everything_else_holds`
/ `...::test_on_pass_still_refused_when_reservation_is_dispatched`
/ `...::test_on_a_provider_exception_is_refused_never_pass` / `...::test_on_an_evaluate_exception_is_refused`
/ `...::test_off_gate_ignores_quality_even_with_block_and_broken_providers`
/ `...::test_manual_on_block_is_409` / `...::test_manual_on_review_is_allowed_and_the_approval_is_logged`
（OFF の既存挙動は `tests/unit/test_pipeline_activities.py` の gate テストが無変更で守る。レポートの永続保存・
DB 上の承認記録・技術 QA / 説明欄の gate 時点での読み出し元は未実装 = 本番で ON にすると全件 REVIEW で拒否）

## AC. v3 Phase 2 の共有契約（ADR-0074。INV-105〜107）

### INV-105 判定語彙 PASS/WARN/REVIEW/BLOCK と厳しさの順序は `contracts/verdict.py` だけが定義する
`Verdict`・`VERDICT_ORDER`・`VERDICT_SEVERITY`・`stricter`・`worst` が唯一の定義。`GeoVerdict` / `QualityVerdict` は
同一オブジェクトの別名で、値と直列化は従来どおり。他のモジュールが同じ語彙を literal で再定義しない。（ADR-0074 §1）
**機械検査**: `tests/architecture/test_verdict_single_source.py::test_only_contracts_verdict_defines_the_verdict_set`
/ `tests/architecture/test_verdict_single_source.py::test_the_detector_catches_a_redefinition`
/ `tests/contract/test_verdict_contract.py::test_the_legacy_names_are_the_one_shared_enum`
/ `tests/contract/test_verdict_contract.py::test_wire_values_are_unchanged`
/ `tests/contract/test_verdict_contract.py::test_existing_models_serialize_identically`

### INV-106 LLM 由来の文字列（地名・ラベル・主張・自由文）は検査結果・レポート・ログ用フィールドに残らない
検査結果が運ぶのは理由コード（enum）・対象 id・証拠参照（id / hash）・検査した値（enum キー + 有限数値）・
信頼できない値の参照（`untrusted_ref`）だけ。`detail` は印字可能 ASCII 500 字以内の固定文で、入力の写しを入れない。
`GeoCheck.log_fields()` / `QualityCheck.log_fields()` は既存の `LOG_FIELDS`（ADR-0040）の名前だけを使う。（ADR-0074 §2）
**機械検査**: `tests/unit/test_untrusted_inputs.py::test_hostile_strings_never_appear_in_a_geo_verification`
/ `tests/unit/test_untrusted_inputs.py::test_hostile_strings_never_appear_in_geo_log_fields`
/ `tests/unit/test_untrusted_inputs.py::test_every_check_the_verifier_makes_carries_a_reason_code`
/ `tests/unit/test_untrusted_inputs.py::test_log_fields_use_only_existing_log_contract_fields`
/ `tests/unit/test_untrusted_inputs.py::test_a_detail_that_could_carry_input_is_refused`
/ `tests/unit/test_untrusted_inputs.py::test_a_hostile_report_is_rejected_without_echoing_it`
（配線されたログ発行側・critic が出す文面の写像は未検査 = 未配線）

### INV-107 品質レポートは、今の artifact・規則版・時刻に結び付いていなければ、PASS でも投稿の根拠にならない
`QualityReport` は対象の `final_video_sha256`・`rule_version`・`generated_at`（UTC aware）・`expires_at`
（`generated_at` より後で `MAX_REPORT_VALIDITY` 以内）・`model`（AI の検査があれば必須）を持つ。
`domain.quality.freshness.check_freshness` は sha256 違い・規則版違い・未来日付・失効（`now >= expires_at`）を
理由コードで拒否し、`evaluate_for_upload` は同じ関数を通す（`current_rule_version` は必須）。時計は注入で、実時計を読まない。
`schema_version` は 2（版 1 は拒否）。（ADR-0074 §3）
**機械検査**: `tests/unit/test_quality_freshness.py::test_a_report_for_another_artifact_hash_is_refused_even_if_pass`
/ `tests/unit/test_quality_freshness.py::test_a_report_for_an_older_rule_version_is_refused`
/ `tests/unit/test_quality_freshness.py::test_expiry_is_judged_against_the_injected_clock`
/ `tests/unit/test_quality_freshness.py::test_an_incoherent_expiry_is_refused_not_trusted`
/ `tests/unit/test_quality_freshness.py::test_an_ai_check_without_a_model_identity_is_refused`
/ `tests/unit/test_quality_freshness.py::test_a_schema_version_1_report_is_refused_as_a_schema_mismatch`
/ `tests/architecture/test_quality_clock_injected.py::test_quality_modules_never_read_the_clock`
（`upload_gate` への配線は INV-104。規則版の「現行値」は `contracts.quality.QUALITY_RULE_VERSION`）

## AD. 投稿の照合と説明欄の構成（ADR-0075。INV-109〜112）

### INV-109 マーカー照合が UNKNOWN のとき、新しい `videos.insert` に進まない
照合の結果は `MarkerLookupState`（`contracts/upload.py`）の `FOUND` / `NOT_FOUND` / `UNKNOWN`。`NOT_FOUND` は最後の
ページまで読み切って無かったときだけ。API 例外・timeout・quota・auth・応答の形の崩れ・ページ上限の打ち切り・
設定回数のうち1回でも失敗があれば `UNKNOWN`。`_reserve` は UNKNOWN で新しいラウンドの予約を作らず
`UploadOutcomeUnknownError`（既存の `needs_input`）を送出する。bytes 送信後の再投稿の承認
（`reconciled_by = operator_reupload_approved`）は照合を試みた時点で `reupload_approval_consumed` に書き換えて
使い切り（照合の前に commit。CAS）、次の実行は運用者の新しい明示承認が無い限り投稿しない（一般的な再開は
新しい投稿の許可ではない）。照合・採用の前に投稿先チャンネルを確かめ、FOUND の動画は自チャンネルの private
でなければ採用も投稿もしない。FOUND は投稿せずその video id を採用し、
NOT_FOUND のときだけ1回投稿する。同じ Activity を何度再実行しても動画は1本以下。
**機械検査**: `tests/unit/test_upload_marker_lookup.py::test_lookup_error_before_round_two_never_inserts`
/ `tests/unit/test_upload_marker_lookup.py::test_malformed_or_partial_lookup_before_round_two_never_inserts`
/ `tests/unit/test_upload_marker_lookup.py::test_one_failed_attempt_is_unknown_even_if_the_next_one_finds_nothing`
/ `tests/unit/test_upload_marker_lookup.py::test_empty_search_success_inserts_exactly_once`
/ `tests/unit/test_upload_marker_lookup.py::test_found_adopts_the_video_without_any_insert`
/ `tests/unit/test_upload_marker_lookup.py::test_repeated_executions_after_an_unknown_make_at_most_one_video`
/ `tests/unit/test_upload_marker_lookup.py::test_a_channel_mismatch_stops_before_the_lookup_and_adopts_nothing`
/ `tests/unit/test_upload_marker_lookup.py::test_a_found_video_that_is_not_private_is_not_adopted_and_nothing_is_uploaded`
/ `tests/unit/test_upload_marker_lookup.py::test_a_found_video_whose_visibility_cannot_be_read_is_not_adopted`
/ `tests/unit/test_upload_marker_lookup.py::test_adapter_videos_list_returning_fewer_items_than_requested_is_unknown`
/ `tests/unit/test_upload_marker_lookup.py::test_a_lookup_error_consumes_the_approval_so_a_plain_resume_never_inserts`
/ `tests/unit/test_upload_marker_lookup.py::test_a_crash_during_the_lookup_still_leaves_the_approval_consumed`
/ `tests/unit/test_upload_marker_lookup.py::test_the_approval_is_consumed_before_the_lookup_so_two_attempts_cannot_both_use_it`
/ `tests/unit/test_upload_marker_lookup.py::test_adapter_page_limit_with_more_pages_is_unknown_not_not_found`
/ `tests/unit/test_upload_marker_lookup.py::test_adapter_malformed_playlist_page_is_unknown`

### INV-110 タグだけの照合の確認ツールは読み取り専用で、marker なし description の flag は既定 OFF のまま
`scripts/verify-tags-only-match.py` は `youtube.readonly` だけの token でのみ動き（他の scope が1つでもあれば API を
呼ぶ前に拒否）、GET の `channels` / `playlistItems` / `videos` だけを呼ぶ。投稿 adapter を import せず、insert / update /
delete の呼び出しを持たない。出力は件数と不一致の video id だけ（title・description・tags・token を出さない）。
一覧を読み切れない・比較対象が無いときは「結論なし」で、100% 一致とは報告しない。`AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER`
は既定 OFF で、compose / deploy / Dockerfile / Makefile / `.env*` のどこにも書かない。本番での有効化は所有者がこの
ツールを実行して 100% 一致を確認した後の別の判断。
**機械検査**: `tests/unit/test_verify_tags_only_match.py::test_verification_code_has_no_write_call_and_never_imports_the_upload_adapter`
/ `tests/unit/test_verify_tags_only_match.py::test_the_write_detector_catches_bypasses`
/ `tests/unit/test_verify_tags_only_match.py::test_listing_without_the_read_only_check_is_refused_before_any_request`
/ `tests/unit/test_verify_tags_only_match.py::test_a_refused_scope_check_does_not_unlock_listing`
/ `tests/unit/test_verify_tags_only_match.py::test_verification_code_names_no_write_scope`
/ `tests/unit/test_verify_tags_only_match.py::test_live_refuses_any_token_that_is_not_readonly_only_before_calling_youtube`
/ `tests/unit/test_verify_tags_only_match.py::test_live_readonly_token_reads_with_get_only_and_reports`
/ `tests/unit/test_verify_tags_only_match.py::test_cli_fixture_full_agreement_exits_zero_and_prints_counts_only`
/ `tests/unit/test_verify_tags_only_match.py::test_a_truncated_listing_is_never_a_conclusion`
/ `tests/unit/test_verify_tags_only_match.py::test_the_description_without_marker_flag_defaults_off`
/ `tests/unit/test_verify_tags_only_match.py::test_no_deploy_artifact_turns_the_description_without_marker_flag_on`

### INV-111 構成 flag OFF の description は、導入前と byte 単位で同じ
`AVP_UPLOAD_DESCRIPTION_COMPOSED`（`contracts/upload.py::UPLOAD_DESCRIPTION_COMPOSITION_ENV`）は既定 OFF で、
`1` / `true` 以外は OFF。OFF、または `composition` を渡さない `build_youtube_metadata` は、台本の全文を使う従来の
description（marker あり・なし、必須部分あり、長文の切り詰め、空本文）と byte 単位で同じ。
**機械検査**: `tests/contract/test_upload_description_golden.py::test_the_composition_flag_is_off_by_default_and_by_any_non_truthy_value`
/ `tests/contract/test_upload_description_golden.py::test_short_with_and_without_marker`
/ `tests/contract/test_upload_description_golden.py::test_japanese_body_is_sanitized_the_same_way`
/ `tests/contract/test_upload_description_golden.py::test_long_body_is_truncated_to_the_same_bytes`
/ `tests/contract/test_upload_description_golden.py::test_required_parts_layout_is_unchanged`
/ `tests/contract/test_upload_description_golden.py::test_empty_body_shapes_are_unchanged`
/ `tests/contract/test_upload_description_golden.py::test_flag_on_without_a_composition_input_is_still_the_legacy_description`
/ `tests/contract/test_upload_description_composition.py::test_flag_off_ignores_the_composition`

### INV-112 構成された description は必須部分（参照・クレジット）を黙って切らず、内部 ID を出さない
`compose_description` は 導入文 → 補足 → 参照 → クレジット → （marker flag OFF のとき）マーカー の順に組む。参照・
クレジット・マーカーを先に確保し、入らなければ `DescriptionBlocked`（`REQUIRED_PARTS_TOO_LONG` / `INTRO_NO_ROOM` /
`REFERENCES_MISSING` / `CREDIT_NOT_REPRESENTABLE` / `INTERNAL_ID`）を返す。切り詰めるのは導入文と補足だけで、
補足はまるごと落とす。帰属文は書き換えない（`<` `>` を含めば BLOCK）。いずれかの部分に内部 ID があれば BLOCK
（marker 行自体は対象外）。`build_youtube_metadata` は BLOCK を `DescriptionBlockedError` で送出する。
**機械検査**: `tests/contract/test_upload_description_composition.py::test_order_is_intro_supplement_references_credits_marker`
/ `tests/contract/test_upload_description_composition.py::test_credit_lines_are_kept_verbatim`
/ `tests/contract/test_upload_description_composition.py::test_credit_with_angle_brackets_blocks_instead_of_being_rewritten`
/ `tests/contract/test_upload_description_composition.py::test_required_references_missing_blocks`
/ `tests/contract/test_upload_description_composition.py::test_required_parts_over_the_limit_block_and_never_truncate`
/ `tests/contract/test_upload_description_composition.py::test_not_enough_room_for_the_intro_blocks`
/ `tests/contract/test_upload_description_composition.py::test_long_optional_parts_are_trimmed_but_required_parts_survive`
/ `tests/contract/test_upload_description_composition.py::test_whole_supplements_are_dropped_from_the_end_not_cut`
/ `tests/contract/test_upload_description_composition.py::test_internal_ids_in_any_part_block`
/ `tests/contract/test_upload_description_composition.py::test_the_final_string_is_linted_too`
/ `tests/contract/test_upload_description_composition.py::test_flag_on_blocked_composition_raises_instead_of_truncating`
/ `tests/contract/test_upload_description_composition.py::test_flag_on_with_marker_removed_keeps_the_lint_and_the_tag`

## AE. BGM の Render 結線（ADR-0077。設定キー `bgm_enabled`、既定 OFF）

番号 INV-105〜112 は他の枝の予約。この節は INV-113〜116。

### INV-113 BGM が OFF（設定が無効、または cue sheet が無い）のとき、描画の同一性と ffmpeg コマンドは BGM 導入前と同一
`render_input_hash` は `bgm=None` のとき BGM のキーを入れず、`build_filtergraph` / `build_argv` は BGM 引数が無いとき
導入前と同じ文字列・argv を返す。期待値は導入前のコミット（6d14c30）で実測した固定値。
BGM を混ぜるときだけ、混合計画（cue ごとの音源 sha・証拠 hash・時刻・フェード・上限適用後の dB・ducking・混合の版）が
input_hash に入り、そのどれを変えても hash が変わる。production の入力 hash には BGM が入らない（INV-116）。
**機械検査**: `tests/unit/test_bgm_off_golden.py::test_input_hash_without_bgm_equals_the_pre_bgm_value`
/ `tests/unit/test_bgm_off_golden.py::test_filtergraph_without_bgm_equals_the_pre_bgm_value`
/ `tests/unit/test_bgm_off_golden.py::test_argv_without_bgm_equals_the_pre_bgm_value`
/ `tests/unit/test_bgm_resolve.py::test_no_cue_sheet_means_not_applied_and_identity_is_the_pre_bgm_hash`
/ `tests/unit/test_bgm_resolve.py::test_a_usable_cue_sheet_changes_the_input_hash_and_each_mix_field_moves_it`
/ `tests/unit/test_bgm_ffmpeg_graph.py::test_bgm_graph_tail_snapshot_and_video_part_is_unchanged`

### INV-114 ライセンスの事実を満たさない曲は鳴らない。1 つでも満たさなければ BGM 全体を使わず、理由を返す
cue sheet が指す曲は、描画のたびに `now` で選曲と同じ検査（`reject_reasons`）と `credits_for` を通る。未承認・失効・取消・
隔離・再利用/商用/改変不可・形式（Shorts 等）不可・期限切れ・カタログに無い・実ファイルの sha256 が違う/読めない・
証拠 hash 不一致・帰属文不一致・cue が時間軸外/未知の scene は、他に曲が無くても使われず、BGM 無しで描画が続く
（拒否で止めない。ADR-0077 §3）。`now` が naive なら例外（黙って通さない）。
**機械検査**: `tests/unit/test_bgm_resolve.py::test_a_track_that_fails_any_license_fact_is_never_used_even_if_it_is_the_only_one`
/ `tests/unit/test_bgm_resolve.py::test_a_track_expiring_after_the_cue_sheet_was_made_is_rejected_at_render_time`
/ `tests/unit/test_bgm_resolve.py::test_a_shorts_only_restriction_follows_the_profile_format`
/ `tests/unit/test_bgm_resolve.py::test_unknown_track_altered_or_missing_file_and_stale_evidence_are_not_used`
/ `tests/unit/test_bgm_resolve.py::test_one_bad_cue_drops_all_of_bgm`
/ `tests/unit/test_bgm_resolve.py::test_naive_now_is_refused`

### INV-115 BGM 入りの完成動画は、実測ラウドネスが範囲内でなければ保存されない。範囲外・無音・実測不能は構造化された不合格
ffmpeg `ebur128=peak=true` の要約から積分ラウドネス・true peak・LRA を 1/100 単位の int で取り出し、理由コード
（`ok` / `too_quiet` / `too_loud` / `true_peak_exceeded` / `silent`）つきの結果にする。要約が読めない・実測が走らない場合は
例外（通さない）。不合格は技術 QA の `bgm_loudness` 項目として `FinalVideoValidationError` になる。許容範囲
（積分 -24〜-9 LUFS、true peak ≤ -1.0 dBTP）は `contracts/music_render.py` の唯一の定義で**未校正**。
**機械検査**: `tests/unit/test_bgm_loudness.py::test_real_ffmpeg_summary_is_parsed_into_centi_units`
/ `tests/unit/test_bgm_loudness.py::test_silent_summary_has_no_peak_and_is_a_silent_failure`
/ `tests/unit/test_bgm_loudness.py::test_unreadable_output_is_an_error_never_a_pass`
/ `tests/unit/test_bgm_loudness.py::test_verdict_reason_codes_and_boundaries`
/ `tests/unit/test_bgm_loudness.py::test_a_failed_verdict_becomes_a_failed_qa_check_that_raises`
/ `tests/unit/test_bgm_loudness.py::test_probe_that_cannot_run_raises_instead_of_passing`
（Activity での実行は `tests/unit/test_render_activities_bgm.py::test_out_of_range_loudness_is_a_structured_failure_and_nothing_is_stored`）

### INV-116 BGM だけの変更は render だけを再実行し、fal・production・予約台帳に触れない。Temporal の Activity 契約は変わらない
BGM の供給元とラウドネス測定は `RenderActivities` のコンストラクタ引数で、Activity の入力ではない。Render の経路（worker 全体）は
fal クライアント・production worker のコードを読み込まない。cue sheet だけを変えた再描画は、fal クライアントの全メソッドを
「呼ばれたら失敗」にしても通り、予約台帳の件数 0・production の Artifact（id / 版 / sha256）不変・job は render のみ。
変更のない再実行は現行 final_video を再利用する（描画 0 回）。BGM 入り動画は証跡（クレジット原文・曲の sha と証拠 hash・
実測ラウドネス・final_video の sha256）を MinIO に保存してから記録され、証跡が無い BGM 入り動画は存在しない。
Activity 名と入出力 dataclass のフィールド（名前・順序）は導入前と同一。
**機械検査**: `tests/unit/test_render_activities_bgm.py::test_bgm_only_changes_rerender_only_and_never_touch_production_or_fal`
/ `tests/unit/test_render_activities_bgm.py::test_bgm_changes_identity_and_stores_evidence_credits_and_loudness`
/ `tests/unit/test_render_activities_bgm.py::test_out_of_range_loudness_is_a_structured_failure_and_nothing_is_stored`
/ `tests/unit/test_render_activities_bgm.py::test_no_cue_sheet_renders_exactly_like_no_bgm_support`
/ `tests/unit/test_render_activities_bgm.py::test_expired_or_unknown_license_renders_without_bgm_and_logs_the_reason`
/ `tests/unit/test_render_activities_bgm.py::test_a_track_that_expires_after_a_bgm_render_is_replaced_by_a_bgm_less_render`
/ `tests/architecture/test_bgm_render_boundaries.py::test_the_render_worker_loads_no_fal_or_production_module`
/ `tests/architecture/test_bgm_render_boundaries.py::test_bgm_modules_do_not_import_production_or_provider_code`
/ `tests/architecture/test_bgm_render_boundaries.py::test_render_activity_names_and_io_dataclass_fields_are_unchanged`
/ `tests/architecture/test_bgm_render_boundaries.py::test_the_setting_key_is_read_only_where_the_worker_is_assembled`
/ `tests/unit/test_bgm_setting.py::test_default_is_off_and_the_key_name_is_single_sourced`
（Temporal の replay は `tests/unit/test_log_old_history_replay.py` 等で確認。**実 Temporal / MinIO / PostgreSQL での通し、
および説明欄・投稿前 Gate への結線は未検査**）

## AF. 歴史地図の正確さと描画（ADR-0078。INV-117〜120 は本枝の予約。純関数と描画部品のみ・未接続）

### INV-117 歴史地図の誤り（位置・時代・国境・根拠の欠落）は理由コード付きの BLOCK になり、緩む経路が無い
`domain.geo.map_accuracy.check_map_accuracy` は Wave 1 の検証結果を `MapAccuracyResult`
（verdict + `MapReasonCode` + 対象 id + claim / source の参照。**自由文なし**）に写す。位置のずれ・軸順の取り違え、
歴史地図でのアナクロな地点・名前・ラベル、現代国境、根拠（claim / 取得済み資料）に結びつかない地点は BLOCK。
同名異所・参照なし・時間基準不明は REVIEW のまま（自動で 1 つに決めない）。verdict は finding の最悪値と
一致しなければ作れず、`escalate` は REVIEW → BLOCK の一方向。
**機械検査**: `tests/unit/test_geo_map_accuracy.py::test_misplaced_coordinates_are_blocked_with_the_place_as_target`
/ `tests/unit/test_geo_map_accuracy.py::test_an_anachronistic_place_name_on_a_historical_map_is_blocked`
/ `tests/unit/test_geo_map_accuracy.py::test_modern_borders_on_a_historical_map_are_blocked`
/ `tests/unit/test_geo_map_accuracy.py::test_a_place_whose_claim_does_not_resolve_is_blocked_as_missing_evidence`
/ `tests/unit/test_geo_map_accuracy.py::test_a_place_whose_source_was_not_fetched_is_blocked_as_missing_evidence`
/ `tests/unit/test_geo_map_accuracy.py::test_a_homonym_stays_a_review_and_is_never_auto_picked`
/ `tests/unit/test_geo_map_accuracy.py::test_the_result_verdict_cannot_be_milder_than_its_worst_finding`
/ `tests/unit/test_geo_map_accuracy.py::test_escalation_never_relaxes_any_verdict`
`check_map_accuracy` は全域関数: 指摘が上限を超える・検証器の結果が契約の上限で作れないときも例外にせず
`TOO_MANY_FINDINGS` の BLOCK を返す:
`tests/unit/test_geo_map_accuracy.py::test_too_many_findings_are_capped_with_a_block_reason`
（QualityGate が呼ぶ配線は未実装。呼び出し側が `evidence_sources` を渡さないと資料の解決は検査されない）

### INV-118 見た目の品質は正確さと別の結果で、正確さを緩めない
`domain.geo.map_presentation.check_map_presentation` は `MapLayout`（幾何）だけを読み、ラベルの重なり・
最小フォント px・コントラスト比・キャンバス内収まりを別の理由コード（`PresentationReasonCode`）で返す。
`combine_map_results` は `stricter` でしか合流せず、`CombinedMapResult` は正確さより緩い verdict を作れない。
模式図の免責が読めない・画面外・無いときだけ見た目で BLOCK（INV-96 の表示要件）。
**機械検査**: `tests/unit/test_geo_map_presentation.py::test_good_presentation_never_upgrades_accuracy`
/ `tests/unit/test_geo_map_presentation.py::test_a_perfectly_legible_inaccurate_map_is_still_blocked`
/ `tests/unit/test_geo_map_presentation.py::test_a_combined_result_cannot_be_milder_than_accuracy`
/ `tests/unit/test_geo_map_presentation.py::test_presentation_findings_use_presentation_codes_not_accuracy_codes`
/ `tests/unit/test_geo_map_presentation.py::test_a_disclaimer_off_canvas_or_unreadable_is_blocked`
（QualityGate が呼ぶ配線は未実装。画像を読み戻した検査はしない: 幾何は描画器の申告。
接続は `combine_map_results(..., manifest_sha256=, render_sha256=)` に `RenderedMap` の値を渡し、
結果が今の画像に結びつくことを確認すること。結びつけの強制は接続する枝で検査する）

### INV-119 同じ GeoManifest は同じ画像のバイト列になり、地図の部品は有料 provider に触れない
`infrastructure/media/map_render.render_map` は manifest・`MapRenderSpec`・フォントのバイト列だけから PNG を作る
（投影は `domain.geo.map_projection`、描画順は place_id 昇順、PNG にメタデータ・時刻なし、乱数なし）。
地図の部品（`contracts/geo_map.py`・`domain/geo/map_*.py`・`infrastructure/media/map_*.py`）は
`infrastructure.providers`・httpx・socket・subprocess・時計・乱数を import せず、描画で fal の module も読み込まれない。
**機械検査**: `tests/unit/test_geo_map_render.py::test_the_same_manifest_renders_to_identical_bytes`
/ `tests/unit/test_geo_map_render.py::test_rendering_in_a_fresh_process_gives_the_same_hash`
/ `tests/unit/test_geo_map_render.py::test_the_order_of_places_and_labels_does_not_change_the_image`
/ `tests/unit/test_geo_map_render.py::test_rendering_makes_no_network_or_fal_calls`
/ `tests/unit/test_geo_map_render.py::test_a_fresh_process_that_renders_a_map_never_loads_a_provider_module`
/ `tests/architecture/test_geo_maps_boundaries.py::test_map_modules_import_no_paid_provider_network_clock_or_random`
/ `tests/architecture/test_geo_maps_boundaries.py::test_the_detector_catches_violating_fixtures`
（Pillow / FreeType の版を上げたら同一性は再確認が要る。バイト列の固定値 golden は版依存なので持たず、
同一入力の再現性で検査する）
グリフ欠落: 選んだフォントに無い字（日本語ラベルを既定フォントで描く等）は `glyphs_missing` として申告され、
見た目の検査が BLOCK（`FONT_MISSING_GLYPHS`）。`tests/unit/test_geo_map_render.py::test_a_non_latin_label_without_a_capable_font_is_not_a_pass`

### INV-120 flag `GEO_MAPS_ENABLED` が OFF（既定）なら、render / storyboard / production の出力は接続前と同じ
`Settings.geo_maps_enabled` の既定は False。OFF の `render_map_scene_visual` は何も描かず `None`。地図の部品は
workflow・Activity・storyboard・production・render の計画側から import されず（Activity の追加・並べ替えと
dataclass の変更は無い）、既存の `render_plan_sha256` / filtergraph は接続前と byte 単位で同じ。
**機械検査**: `tests/unit/test_geo_maps_flag_off.py::test_the_flag_defaults_to_off_and_is_read_from_the_environment`
/ `tests/unit/test_geo_maps_flag_off.py::test_with_the_flag_off_nothing_is_rendered`
/ `tests/unit/test_geo_maps_flag_off.py::test_the_flag_is_not_exposed_in_any_deployment_file`
/ `tests/unit/test_geo_maps_flag_off.py::test_render_identity_and_filtergraph_are_byte_identical_to_before`
/ `tests/architecture/test_geo_maps_boundaries.py::test_production_side_does_not_import_the_map_parts`
（flag ON の挙動は部品の呼び出しまで。ON にしても workflow には何も起きない。接続は後続 →
ADR-0084 が既存 Activity の内側へ結線した。OFF の同一性は INV-143 が引き継ぐ）

## AG. v3 Phase 3 の安全（ADR-0079 / ADR-0080。INV-121〜125 は Phase 3 監査枝の予約）

### INV-121 integration テストは、隔離の積極的な宣言が無い限り、接続先が設定されていれば収集時に例外で止まる
接続先（`TEMPORAL_ADDRESS` / `TEST_DATABASE_URL` / `DATABASE_URL` / `MINIO_ENDPOINT`）が1つでも設定されているとき、
`AVP_INTEGRATION_ISOLATION` が `apptest:avp2-oslog-c*`（接続先は内部サービス名 `temporal` / `postgres` / `minio` のみ、
このホスト自身へ解決される名前は不可）か `ci-ephemeral`（`GITHUB_ACTIONS=true` と loopback）の**完全一致**でなければ
`ProductionTargetError`。綴り違い・空・本番 project `avp2`・前後の空白はすべて拒否（許可リスト。拒否リストではない）。
**機械検査**: `tests/unit/test_production_target_guard.py::test_a_target_without_an_isolation_declaration_is_refused`
/ `tests/unit/test_production_target_guard.py::test_a_mistyped_declaration_fails_closed`
/ `tests/unit/test_production_target_guard.py::test_the_ci_declaration_is_refused_outside_github_actions`
/ `tests/unit/test_production_target_guard.py::test_the_apptest_declaration_never_allows_the_host_or_its_published_ports`
/ `tests/unit/test_production_target_guard.py::test_the_production_compose_project_is_never_a_valid_declaration`
/ `tests/unit/test_production_target_guard.py::test_the_isolated_apptest_stack_passes`
/ `tests/unit/test_production_target_guard.py::test_the_ci_ephemeral_services_pass`
/ `tests/architecture/test_integration_no_hardcoded_addresses.py::test_integration_conftest_asserts_isolation_before_anything_else`

### INV-122 integration テストは接続先を直書きせず、到達した Temporal が本番（日次 Schedule が在る）なら拒否する
`tests/integration/` と `tests/support/` に `localhost:7233` 等・`TEMPORAL_ADDRESS = "..."`・
`connect_test_client("...")`・DB URL の host の直書きが無い。Temporal は `default` namespace の
`avp-daily-episode` を describe し、在れば `ProductionTemporalError`、NOT_FOUND 以外で判定できなければ拒否（fail closed）。
**機械検査**: `tests/architecture/test_integration_no_hardcoded_addresses.py::test_no_hardcoded_connection_address`
/ `tests/architecture/test_integration_no_hardcoded_addresses.py::test_the_detector_catches_the_original_incident`
/ `tests/unit/test_temporal_production_probe.py::test_a_server_that_has_the_daily_schedule_is_production`
/ `tests/unit/test_temporal_production_probe.py::test_a_server_without_the_schedule_passes`
/ `tests/unit/test_temporal_production_probe.py::test_an_undecidable_answer_fails_closed`
（PostgreSQL / MinIO を本番と区別する読み取りだけの指標は無い。静的な宣言と接続先の規則に依存する: ADR-0079 §2）

### INV-123 承認記録は種別（所有者本人 / エージェントへの委任）を述べ、述べない記録・委任は所有者専用の関門を通さない
承認の参照は `OWNER-<yyyymmdd>-<id>`（所有者本人）か `AGENT-<yyyymmdd>-<id>:OWNER-<yyyymmdd>-<id>`（委任。委任元の
所有者の指示を必ず書く）。種別を述べない・形が違う・日付が実在しない参照は承認ではない。所有者専用の関門
（`domain.approval.OwnerGate`: 新機能 ON / fal 予算を上げる / public・unlisted 投稿 / 保留の解放・再開 / 予約の手動確定）は
`AGENT_DELEGATED` では通らない。release 承認記録は `approval_ref` が必須（無ければ UNKNOWN）で、`enables` に所有者専用の関門を
書いた委任は BLOCKED。既存の文字列列（`provider_breakers.approved_by` / 予約照合の `actor`）には `<kind>:<name>` で保存し、
種別の接頭辞が無い旧い値は所有者承認とは読まない（DB の変更なし）。種別の語彙は `contracts/approval.py::ApproverKind` の1箇所。
認証ではない（誰でも書ける形式検査）: 目的は監査記録で所有者の判断と委任の範囲の判断を取り違えないこと。
**機械検査**: `tests/unit/test_approval_kind.py::test_anything_that_does_not_state_a_valid_kind_is_not_an_approval`
/ `tests/unit/test_approval_kind.py::test_an_agent_delegated_approval_never_satisfies_an_owner_gate`
/ `tests/unit/test_approval_kind.py::test_the_owner_only_gates_are_the_ones_that_spend_or_publish`
/ `tests/unit/test_approval_kind.py::test_the_stored_actor_states_its_kind_and_a_legacy_value_does_not`
/ `tests/unit/test_release_approval_kind.py::test_a_record_that_does_not_state_its_kind_is_not_an_approval`
/ `tests/unit/test_release_approval_kind.py::test_a_delegated_approval_cannot_enable_an_owner_gate`
/ `tests/unit/test_release_approval_kind.py::test_provider_credit_actions_accept_only_the_owners_own_reference`
/ `tests/unit/test_release_approval_kind.py::test_manual_reservation_resolution_accepts_only_the_owners_own_reference`
/ `tests/unit/test_release_approval_kind.py::test_the_release_reservation_waiver_stays_owner_only`

### INV-124 API の変更系ルートは全て bearer token を要求し、未設定・不備なら閉じる（無認証に倒れない）
`POST` / `PUT` / `PATCH` / `DELETE` の全ルート（OpenAPI から列挙。router を足してもここに入る）は、token 未設定
（ファイル無し・読めない・壊れている・0600 以外・短い token・重複）なら 503、token が無い・違う（`Bearer` 以外、前後の
空白、1 文字違い）なら 401、いずれも handler に到達せず workflow を起動しない。比較は digest を全件 `hmac.compare_digest`
（打ち切らない）。読み取り（GET）は開けたまま。token は応答・ログに出ず、actor（`owner` / `agent`）だけが残る。
**機械検査**: `tests/unit/test_api_auth.py::test_every_mutating_route_refuses_when_no_token_is_configured`
/ `tests/unit/test_api_auth.py::test_every_mutating_route_refuses_a_missing_or_wrong_credential`
/ `tests/unit/test_api_auth.py::test_a_valid_token_reaches_the_handler`
/ `tests/unit/test_api_auth.py::test_read_only_routes_stay_open`
/ `tests/unit/test_api_auth.py::test_a_missing_or_malformed_token_file_means_nothing_is_authenticated`
/ `tests/unit/test_api_auth.py::test_a_token_file_readable_by_others_is_refused`
/ `tests/unit/test_api_auth.py::test_comparison_is_constant_time_and_checks_every_entry`
/ `tests/unit/test_api_auth.py::test_the_credential_never_appears_in_a_refusal`
/ `tests/unit/test_api_auth.py::test_every_authenticated_mutation_logs_its_actor_without_the_token`
/ `tests/unit/test_api_auth.py::test_every_operator_script_that_posts_to_the_api_sends_a_token`

### INV-125 品質ゲート ON の手動投稿は、どのトークンでも BLOCK を迂回できず、REVIEW の承認は owner トークンの要求に結び付く（意図の記録）
`AVP_QUALITY_GATE_ENFORCE` ON のとき、`POST /episodes/{id}/upload` は BLOCK・レポート作成失敗を 409 にし（owner/agent
どちらのトークンでも）、REVIEW は owner トークンのときだけ許可して actor 付きでログに残す。agent トークンの REVIEW は
409 `quality: review_requires_owner`。flag OFF の挙動は変えない（ADR-0076）。
**これは意図の記録であって強制ではない**: owner と agent の token が同じファイルにあり、エージェントの OS ユーザーが
読めるなら、エージェントは owner token を使える。owner token を別ファイル（`AVP_API_OWNER_TOKEN_PATH`）に置き、
エージェントが読めない権限にした構成だけが実効を持つ（docs/operations/api-auth.md の「残るリスク」）。BLOCK を
迂回できないこと（トークンの種別に依らない）は強制である。
**機械検査**: `tests/unit/test_api_auth.py::test_manual_upload_cannot_bypass_a_block_with_any_token`
/ `tests/unit/test_api_auth.py::test_review_is_approved_by_the_owner_token_but_never_by_the_agent_token`
/ `tests/unit/test_api_auth.py::test_a_passing_report_lets_the_agent_token_upload`

（INV-123 追補）release 承認の `enables` は必須（明示的な空 = 新機能 OFF）。preflight は対象環境の `.env` の新機能 flag
（`contracts/release.py::FEATURE_FLAG_ENV_KEYS`: `AVP_QUALITY_GATE_ENFORCE` / `BGM_ENABLED` / `GEO_MAPS_ENABLED` /
`AVP_UPLOAD_DESCRIPTION_COMPOSED`）を読み、1 つでも ON なら委任された承認を BLOCKED にする。
**機械検査**: `tests/unit/test_release_feature_flags.py::test_enables_is_mandatory_even_for_an_owner_approval`
/ `tests/unit/test_release_feature_flags.py::test_a_delegated_approval_is_blocked_when_any_flag_is_on_in_the_target`
/ `tests/unit/test_release_feature_flags.py::test_an_owner_approval_with_a_flag_on_must_declare_enable_new_feature`
/ `tests/unit/test_release_collectors_flags_and_tokens.py::test_a_process_environment_that_differs_from_the_dotenv_is_unknown`

（INV-124 追補）deploy 前の release preflight は API の token ファイルの存在・0600・所有者（`AVP_UID`）・owner と agent の
token を確かめる。compose が渡す。読むのは変更系のときだけ。owner を別ファイルにしたとき、共有ファイルの `owner:` 行は拒否。
**機械検査**: `tests/unit/test_release_api_token_check.py::test_an_absent_file_blocks_the_deploy`
/ `tests/unit/test_release_api_token_check.py::test_a_file_the_api_would_reject_blocks_the_deploy`
/ `tests/contract/test_api_token_wiring.py::test_the_secret_directory_is_mounted_read_only_with_a_safe_default`
/ `tests/unit/test_api_auth.py::test_with_an_owner_file_an_owner_token_in_the_shared_file_is_refused`
/ `tests/unit/test_api_auth.py::test_read_requests_never_read_the_token_file_or_log_about_it`
/ `tests/contract/test_apptest_project_pattern.py::test_run_e2e_uses_the_same_prefix_and_pattern`

## AH. 品質検査の記録・必須の集合・手動承認（ADR-0081。INV-126〜132。`AVP_QUALITY_GATE_ENFORCE` の背後）

### INV-126 検査の記録は、今の final_video の hash・入力 hash・規則版・時刻に結び付くものだけが数えられる
記録（`quality_check_records`）は対象 hash（現行 final_video の `artifact_metadata.sha256`）・`input_hash`・
`rule_version`・`checked_at`/`expires_at`（有効期間は `MAX_REPORT_VALIDITY` 以内）を持つ追記専用の行で、
再検査は世代を上げた新しい行。別 hash・別入力・別規則版・期限切れ・世代交代後の記録は「無い」と同じに扱われる。
読み出しは `(episode_id, 対象 hash)` を必ず指定し、「episode の最新」を返す関数が無い。取得した行は SQL の絞り込みと
独立に `select_fresh_check_sets` が再検証する。DB 側は sha 長・期限・語彙の CHECK、`(episode, sha, 規則版, key, 世代)`
の一意、UPDATE 拒否トリガ（PostgreSQL）で構造的に守る。
**機械検査**: `tests/unit/test_quality_records.py::test_a_record_for_another_final_video_hash_is_not_counted`
/ `...::test_a_record_for_an_older_rule_version_is_not_counted` / `...::test_an_expired_record_is_not_counted_and_the_boundary_is_exclusive`
/ `...::test_the_newest_generation_wins_and_a_stale_newest_hides_the_older_one`
/ `tests/unit/test_quality_gate_service.py::test_a_new_final_video_gets_none_of_the_old_records_or_declarations`
/ `...::test_expired_records_are_not_reused_and_are_regenerated` / `...::test_a_tampered_final_video_body_is_never_trusted`
/ `tests/integration/test_quality_records_postgres.py::test_records_are_append_only_on_postgres`
/ `...::test_stale_and_foreign_records_are_refused_end_to_end`

### INV-127 必須の検査が揃わない・使用宣言が無いものは PASS にならない
必須 = 技術 QA（`VISUAL`・`AUDIO`）・説明欄 lint（`METADATA`）。BGM（`MUSIC`・`LICENSE`）・地図（`GEO`）は render の
使用宣言（`quality_usage_declarations`）が `used=true` なら必須、`used=false` なら明示の不使用記録（`used=true` は
`evidence_ref` 必須の CHECK）。**宣言が無いことは不使用ではない**（`USAGE_DECLARATION_MISSING` の REVIEW）。必須の
セットが無い・古い・producer が失敗した、は `REQUIRED_CHECK_MISSING` の決定論の REVIEW（ADR-0073 §2）。セットは対応する
gate を過不足なく含まなければ書けない・読んでも無効。技術 QA は必須 19 項目のどれが欠けても該当 gate が BLOCK。
**機械検査**: `tests/unit/test_quality_records.py::test_an_undeclared_feature_is_refused_not_treated_as_unused`
/ `...::test_a_used_feature_requires_its_check_set` / `...::test_each_always_required_check_is_required`
/ `...::test_a_set_must_cover_exactly_the_gates_of_its_key`
/ `tests/unit/test_quality_description_and_qa_inputs.py::test_dropping_any_required_check_blocks_its_own_gate_only`
/ `tests/unit/test_quality_gate_service.py::test_without_usage_declarations_the_gate_refuses_as_review`
/ `...::test_a_failing_producer_leaves_the_set_missing_and_the_gate_refuses`

### INV-128 gate が組み立てた品質レポートは、拒否も含めて対象 hash に結び付けて保存される。保存できなければ許可しない
`quality_reports` に `QualityReport.to_dict()`・その正準 hash・対象 hash・入力 hash・規則版・判定を残す（追記専用）。
レポートを保存できなかったときは `REPORT_PRODUCTION_FAILED` で拒否する（監査が残らない許可を出さない）。
**機械検査**: `tests/unit/test_quality_gate_service.py::test_the_report_is_persisted_bound_to_the_video_hash_even_when_refused`
/ `...::test_when_the_report_cannot_be_saved_the_upload_is_refused`
/ `tests/unit/test_quality_repositories.py::test_the_stored_report_is_the_exact_serialisation`

### INV-129 REVIEW を手動で解放できるのは所有者の承認だけで、承認は誰が・いつ・どの hash・どの報告かを記録してから進む
`apps/api/auth.py::get_upload_approver`（未認証は `None`、owner トークンは `OWNER`、agent トークンは `AGENT_DELEGATED`）が承認者を与える。`None`・不明・種別が `OWNER` 以外（`AGENT_DELEGATED` を含む）は
403 で、workflow を起動しない。`OWNER` なら `quality_manual_approvals`（`approver_kind`・`approver_id`・`approved_at`・
対象 hash・`report_id`。(report_id, sha) の複合 FK）を書いてから起動し、書けなければ起動しない（409）。BLOCK・作成失敗・
欠落は承認で解放できない。PASS / WARN は承認者を要しない。
**機械検査**: `tests/unit/test_quality_gate_persisted_wiring.py::test_manual_review_without_an_approver_is_forbidden_and_starts_nothing`
/ `...::test_manual_review_by_a_delegated_agent_is_forbidden` / `...::test_manual_review_by_the_owner_records_the_approval_then_starts`
/ `...::test_when_the_approval_cannot_be_recorded_nothing_is_started`
/ `tests/unit/test_quality_approval_and_wiring.py::test_anything_unknown_is_refused_and_never_raises`
/ `tests/unit/test_quality_gate_service.py::test_only_a_known_owner_can_have_an_approval_recorded`

### INV-130 `AVP_QUALITY_GATE_ENFORCE` が OFF（既定）のとき、gate・手動経路は検査記録を読まず書かず、挙動は導入前と同一
OFF ではサービスを組み立てず（`build_quality_service` は `None`。MinIO クライアントも作らない）、`upload_gate`・
`start_upload` は記録の表に触れない。migration 0019 を適用しても OFF の挙動は変わらない。ON でもサービスが結線されていない
経路（既定の `get_quality_service`）は ADR-0076 の従来の経路のまま。
**機械検査**: `tests/unit/test_quality_gate_persisted_wiring.py::test_off_never_touches_the_service_or_the_quality_tables`
/ `...::test_off_leaves_the_manual_path_untouched` / `...::test_the_default_dependencies_are_the_pre_existing_path_and_no_approver`
/ `tests/unit/test_quality_approval_and_wiring.py::test_with_the_flag_off_no_service_is_built_and_no_store_is_created`
（OFF の既存挙動は `tests/unit/test_pipeline_activities.py`・`tests/unit/test_quality_gate_wiring.py` が無変更で守る）

### INV-131 gate が lint する説明欄は、upload 工程が送る説明欄と同じ関数・同じ入力で組まれる
`domain/upload/metadata_builder.py::build_upload_metadata` が唯一の組み立て口で、upload 工程と gate が呼ぶ。
`AVP_UPLOAD_DESCRIPTION_COMPOSED` が OFF のとき結果は従来の `build_youtube_metadata` 呼び出しと byte 単位で同じ。
gate は marker 行（意図した内部 ID）だけを除いて lint し、組み立てが BLOCK なら `METADATA_LINT_FAILED`。
**機械検査**: `tests/unit/test_quality_description_and_qa_inputs.py::test_with_the_flag_off_the_builder_is_byte_identical_to_the_previous_call`
/ `...::test_with_the_flag_on_the_description_is_composed_from_the_script`
/ `...::test_an_internal_id_in_the_narration_blocks_the_composed_description`
（既存の `tests/unit/test_upload_activities.py` が upload 工程を無変更で守る）

### INV-132 migration 0019 は expand-only で、旧コードが先に動き続ける
新しい表 4 つ（`quality_check_records`・`quality_usage_declarations`・`quality_reports`・`quality_manual_approvals`）を
足すだけで、既存の表・制約・索引・列に触れない。release 走査（`scan_migration`）が何も検出しない。downgrade は行が
残っていれば拒否する。語彙の凍結値は契約と一致し、DB の有効期間 24h は `MAX_REPORT_VALIDITY` と一致する。
**機械検査**: `tests/contract/test_migration_0019_quality.py`（全件）/ `tests/contract/test_migration_matches_models.py`
/ `tests/integration/test_quality_records_postgres.py::test_migration_0019_upgrade_and_downgrade_on_postgres`

## AI. BGM のカタログ・配置・ナレーション優先・品質検査（ADR-0083。`BGM_ENABLED` の背後、既定 OFF）

番号 INV-133〜138 は Phase 3 BGM 枝の予約。INV-121〜132 は他の枝の予約。

### INV-133 曲の同一性は内容の sha256。同じ内容は再登録されず、承認は人の証拠なしに生まれない
カタログの登録（`infrastructure/music/catalog_store.register_track`）は `ingest_check`（純粋）の判定どおり:
同じ sha256 は `DUPLICATE`（何も書かず既存の曲・承認事実をそのまま使う。別名での再申告も新しい承認を作らない）、
申告 sha と実バイトの不一致・`track_id` の内容衝突・content-addressed でない `object_key` は `REJECT`（何も書かない）。
原本が名前と違う内容・壊れたカタログは `CatalogStoreError` で止まり上書きしない。書き手（登録）と読み手
（`LocalDirectoryBgmSource`）が同じ配置を共有する。検証用の曲は標準ライブラリだけで合成し、第三者の音源を使わない
（`source_kind=original` / `license_type=custom`）。
**機械検査**: `tests/unit/test_bgm_catalog_store.py::test_registering_writes_the_content_addressed_original_and_the_catalog`
/ `tests/unit/test_bgm_catalog_store.py::test_the_same_bytes_are_reused_not_registered_again`
/ `tests/unit/test_bgm_catalog_store.py::test_a_lying_sha_or_a_swapped_track_id_is_rejected_and_nothing_is_written`
/ `tests/unit/test_bgm_catalog_store.py::test_a_tampered_original_is_not_silently_accepted`
/ `tests/unit/test_bgm_catalog_store.py::test_a_corrupt_catalog_stops_registration_instead_of_overwriting`
/ `tests/unit/test_bgm_catalog_store.py::test_what_is_written_is_what_the_render_source_reads_back`
/ `tests/unit/test_bgm_synth.py::test_the_same_arguments_give_the_same_bytes_and_the_expected_length`
（object storage / DB への載せ替えは未実装。供給元は作業ディレクトリのファイル。実在の曲の登録は所有者の音源と証拠が要る）

### INV-134 選曲と配置は決定的で、法的に通らない曲は選ばれず、silent の scene には音楽が掛からない
`choose_track` は `select_approved`（INV-97）の後に energy を同順位内の並びにだけ使う安定ソートで、同じ入力は同じ曲、
入力の並びに依らない。ライセンスの事実を満たさない曲は他に候補が無くても返さない。`plan_cue_sheet` は `RenderPlan` と役割
（intro/body/climax/outro/silent）から `MusicCueSheet` を作る純粋関数で、silent の scene に cue を掛けず、連続区間では
`source_offset_ms` で scene をまたいで同じ曲を続け、曲の実測ラウドネスが未測定・未承認・全 silent・未知の scene は配置を拒否する
（推測しない）。gain は常に `BGM_NARRATION_PRIORITY_MAX_DB` 以下。作った cue sheet は描画側の再検査（`resolve_bgm`）を通る。
**機械検査**: `tests/unit/test_bgm_placement.py::test_the_same_inputs_give_the_same_cue_sheet`
/ `tests/unit/test_bgm_placement.py::test_default_roles_open_with_intro_and_close_with_outro_and_the_sheet_is_resolvable`
/ `tests/unit/test_bgm_placement.py::test_one_continuous_run_keeps_the_same_music_position_across_scene_boundaries`
/ `tests/unit/test_bgm_placement.py::test_a_silent_scene_has_no_music_and_the_music_restarts_after_it`
/ `tests/unit/test_bgm_placement.py::test_roles_set_the_level_and_climax_is_louder_than_body_but_never_above_the_cap`
/ `tests/unit/test_bgm_placement.py::test_placement_refuses_instead_of_guessing`
/ `tests/unit/test_bgm_placement.py::test_choice_is_deterministic_and_prefers_mood_then_less_used_then_energy`
/ `tests/unit/test_bgm_placement.py::test_choice_never_returns_a_track_that_fails_a_license_fact`
/ `tests/unit/test_bgm_ffmpeg_graph.py::test_source_offset_starts_the_track_mid_way_and_zero_keeps_the_chain_unchanged`
（`plan_cue_sheet` を呼ぶ運用側＝cue sheet を置く者は未配線）

### INV-135 ナレーション優先は発話中の「声 - BGM」の差を実測して判定する。測れていないものは合格にしない
`FfmpegSpeechMarginProbe` が本番と同じ filtergraph 部品（fade・level・ducking）で「ナレーションだけ」「BGM だけ」を書き出し、
各台本 scene の発話区間で積分ラウドネスを測る。`evaluate_speech_intelligibility`（純粋）は最小の差が
`SPEECH_MARGIN_MIN_CENTI_LU`（`contracts/music_render.py` の唯一の定義、12 LU）以上で合格。BGM が鳴っていない区間は不合格にせず、
声が測れない区間だけでは合格しない。閾値と ducking 係数の根拠は ADR-0083 §5（本番 Episode の声 5 本 + 合成曲で校正、
**1 Episode・合成曲 1 つ**）。
**機械検査**: `tests/unit/test_bgm_intelligibility.py::test_margin_is_voice_minus_music_and_the_smallest_one_decides`
/ `tests/unit/test_bgm_intelligibility.py::test_one_scene_below_the_threshold_fails_the_whole_episode`
/ `tests/unit/test_bgm_intelligibility.py::test_a_scene_without_music_is_not_a_failure_but_unmeasured_voice_is_not_a_pass`
/ `tests/unit/test_bgm_intelligibility.py::test_the_music_stem_reuses_the_real_bgm_chain_and_leaves_nothing_dangling`
/ `tests/unit/test_bgm_intelligibility.py::test_the_voice_stem_is_the_narration_bus_of_the_bgm_less_render`
/ `tests/unit/test_bgm_real_ffmpeg_margin.py::test_ducking_lowers_the_music_under_speech_and_the_probe_measures_it`
/ `tests/architecture/test_bgm_phase3_boundaries.py::test_the_calibrated_constants_are_defined_only_in_the_contract`
（Activity での実測・保存は未配線）

### INV-136 BGM 入り動画のラウドネス不合格は needs_input の失敗のまま。BGM 無しへ自動で落とさない
ADR-0077 §5（INV-115）を維持する。不合格は永続的な失敗（`FinalVideoValidationError` は `NeedsInputError`）で再試行しない。
運用者の逃げ道は `BGM_ENABLED=false`（または cue sheet を外す）で、導入前と同じ描画に戻る。品質検査（INV-138）は保存された
判定の真偽ではなく**数値から範囲を再計算する**。
**機械検査**: `tests/unit/test_render_activities_bgm.py::test_out_of_range_loudness_is_a_structured_failure_and_nothing_is_stored`
/ `tests/unit/test_bgm_quality_checks.py::test_out_of_range_loudness_in_the_evidence_is_blocked_on_the_music_gate`
/ `tests/unit/test_bgm_quality_checks.py::test_the_check_recomputes_loudness_from_numbers_and_does_not_trust_the_stored_flag`
/ `tests/unit/test_bgm_quality_checks.py::test_the_local_loudness_rule_agrees_with_evaluate_loudness_everywhere`

### INV-137 BGM だけの変更は render だけを再実行し、fal を 1 回も呼ばず、production の成果物・入力 hash を動かさない
曲・配置・役割のどれを変えても render の input_hash だけが変わる（BGM 無し + 3 通りが全て別）。fal クライアントの全メソッドを
「呼ばれたら失敗」にして呼び出し回数 0、production の成果物（id・版・sha256・入力 hash・content_fingerprint）と
予約台帳（0 件）と job の種類（render のみ）が不変、描画した回数は変更の数だけ、変更なしは再利用。配置の部品は production /
provider を import しない。INV-116 の一般形を、本物の配置関数が作った cue sheet で検査する。
**機械検査**: `tests/unit/test_bgm_only_change.py::test_changing_only_the_track_or_the_placement_reruns_render_and_nothing_else`
/ `tests/architecture/test_bgm_phase3_boundaries.py::test_phase3_bgm_modules_do_not_import_production_or_provider_code`
/ `tests/architecture/test_bgm_phase3_boundaries.py::test_the_render_worker_still_loads_no_fal_or_production_module_with_the_new_parts`

### INV-138 `bgm_quality_checks` は純粋で fail-closed。使っていないことも、その動画に結び付いた PASS で明示する
BGM を使っていない（証跡なし）なら MUSIC と LICENSE の両方に、`evidence_ref` = final_video の sha256 の PASS を返す
（検査が無いことを PASS にしない）。使っているなら、証跡が今の動画のもの・実測ラウドネスが範囲内（数値から再計算）・発話中の差が閾値以上
（未測定は REVIEW）・曲ごとのライセンスが今も有効で音源 sha と証拠 hash が一致・クレジット原文が一致、のどれが崩れても BLOCK。
`detail` は固定の ASCII 文で ID や原文を持たない。`now` が naive なら例外。「証跡が無い」と「証跡を読めなかった」の区別は
呼び出し側の責務（後者は例外にして gate を止める）。
**機械検査**: `tests/unit/test_bgm_quality_checks.py::test_not_used_is_an_explicit_pass_bound_to_the_final_video_hash_for_both_gates`
/ `tests/unit/test_bgm_quality_checks.py::test_a_clean_bgm_video_passes_every_check_and_each_is_bound_to_the_evidence`
/ `tests/unit/test_bgm_quality_checks.py::test_credit_lines_must_equal_the_catalog_text`
/ `tests/unit/test_bgm_quality_checks.py::test_evidence_of_another_video_is_blocked_on_both_gates`
/ `tests/unit/test_bgm_quality_checks.py::test_a_track_no_longer_valid_or_changed_since_rendering_is_blocked`
/ `tests/unit/test_bgm_quality_checks.py::test_a_track_missing_from_the_catalog_is_blocked`
/ `tests/unit/test_bgm_quality_checks.py::test_speech_margin_unmeasured_is_review_and_too_small_is_block`
/ `tests/unit/test_bgm_quality_checks.py::test_details_are_fixed_ascii_and_never_echo_ids_or_text`
/ `tests/unit/test_bgm_quality_checks.py::test_naive_now_and_missing_evidence_hash_are_refused`
（永続化と gate への接続は未配線＝`claude/v3-p3-quality` の責務）

## AJ. 歴史地図の Production / Render 結線（ADR-0084。INV-139〜145）

### INV-139 地図の座標は gazetteer だけから決まり、同じ依頼は同じ manifest になる
`GeoMapRequest`（座標を持たない依頼。`extra=forbid`）から `domain.geo.manifest_builder.build_manifest` が
gazetteer の座標・地物の種類・存在期間・不確かさを埋める。依頼の作者（人・LLM）は座標を書けない。gazetteer に無い
`ref_id`・重複・根拠（claim / source）が空の地点は manifest を作らず BLOCK（推測で埋めない）。順序が違う依頼も同じ manifest。
**機械検査**: `tests/unit/test_geo_p3_gate.py::test_a_request_cannot_carry_coordinates`
/ `tests/unit/test_geo_p3_gate.py::test_a_well_formed_request_passes_and_takes_coordinates_from_the_gazetteer`
/ `tests/unit/test_geo_p3_gate.py::test_the_same_request_always_builds_the_same_manifest`
/ `tests/unit/test_geo_p3_gate.py::test_an_unknown_gazetteer_reference_is_blocked_not_guessed`
/ `tests/unit/test_geo_p3_gate.py::test_a_place_without_a_claim_or_without_a_source_is_blocked`
（ADR-0084 §1。完成済みの manifest を直接受ける経路は残り、位置違いは検証器が止める:
`test_a_manifest_with_wrong_coordinates_is_blocked_by_the_verifier`）

### INV-140 検証を通らない地図（BLOCK も REVIEW も）は使われず、投稿を許さない
位置違い・軸の取り違え・根拠不足（claim / source が無い・解決しない・取得失敗・評価が足りない）・時代錯誤（名前・
存在期間・国境）・時間基準不明・同名の曖昧は、`gate_map` が PASS にしない。PASS でない地図は描かれず（見た目だけが
不合格の画像は証跡に残るが使われない）、Production の `plan` が有料呼び出しの前に `GeoMapRefusedError` で止め、
Render は差し替えず、`map_quality_checks` が PASS でない検査を返す。
**機械検査**: `tests/unit/test_geo_p3_gate.py::test_a_modern_name_on_a_twelfth_century_map_is_an_anachronism`
/ `tests/unit/test_geo_p3_gate.py::test_a_source_that_is_not_in_the_research_evidence_is_refused`
/ `tests/unit/test_geo_p3_gate.py::test_an_insufficient_claim_is_not_evidence`
/ `tests/unit/test_geo_p3_gate.py::test_a_manifest_with_wrong_coordinates_is_blocked_by_the_verifier`
/ `tests/unit/test_geo_p3_gate.py::test_an_unknown_time_basis_on_a_historical_map_is_refused`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_a_refused_map_is_never_drawn`
/ `tests/unit/test_production_plan_geo.py::test_a_map_that_would_be_refused_stops_production_before_any_paid_call`
/ `tests/unit/test_render_activities_geo.py::test_a_refused_map_keeps_the_original_video_and_the_evidence_blocks_the_upload`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_refused_maps_are_not_passes`

### INV-141 使われた地図は hash の鎖で、manifest・検証・画像・クリップ・完成動画に結びつく
`GeoMapRecord` は manifest_sha256 / verification_sha256 / png_sha256 / clip_sha256 / final_video_sha256 と判定の入力
（gazetteer・根拠・下地・フォント）の指紋を束ねる。`binding_problems` は manifest と verification を**再計算**し、PASS の
記録がクリップ・完成動画を欠く／別の完成動画のものなら問題を返す。`verdict` は正確さと見た目の厳しい側と一致しなければ
作れず、PASS は presentation・manifest・画像なしに作れない。Render の証跡は final_video の行より前に保存される。
**機械検査**: `tests/unit/test_geo_p3_record_and_quality.py::test_the_digest_format_matches_the_renderers_manifest_hash`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_tampering_with_the_manifest_or_the_verification_breaks_the_binding`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_a_pass_record_needs_a_clip_and_the_same_final_video`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_the_record_refuses_to_pass_without_the_presentation`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_the_record_refuses_a_verdict_milder_than_its_findings`
/ `tests/unit/test_render_activities_geo.py::test_a_verified_map_replaces_that_scenes_video_and_is_bound_to_the_final_video`
/ `tests/unit/test_render_activities_geo.py::test_no_final_video_row_exists_without_its_geo_evidence`

### INV-142 `map_quality_checks` は純関数で fail-closed。地図なしは完成動画に結びついた明示の PASS
`domain.geo.quality_checks.map_quality_checks` は証跡を `QualityCheck`（gate=GEO・決定論）にするだけ。地図なしは
`evidence_ref = sha256({"geo":"not_used","final_video_sha256":…})` の PASS。指定したのに証跡が無い・複数・鎖の切れ・
別の完成動画の証跡は BLOCK。`detail` に入力の文字列を入れない。
**機械検査**: `tests/unit/test_geo_p3_record_and_quality.py::test_an_episode_without_a_map_gets_an_explicit_hash_bound_pass`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_a_specified_map_with_no_evidence_is_blocked_not_treated_as_not_used`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_evidence_for_another_final_video_is_blocked`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_two_records_for_one_scene_are_ambiguous`
/ `tests/unit/test_geo_p3_record_and_quality.py::test_the_checks_never_echo_input_strings_and_are_deterministic`
/ `tests/architecture/test_geo_boundaries.py::test_the_geo_domain_imports_no_io_clock_or_network_modules`

### INV-143 `GEO_MAPS_ENABLED` が OFF（既定）なら出力は接続前と同じで、workflow・Activity の履歴は変わらない
OFF では `render_input_hash` に `geo` キーが**入らず**（既存の golden hash と一致）、worker は結線を作らず、
Production の事前検査も Render の差し替えも走らない。workflow・Activity の追加/順序変更・入出力 dataclass の変更は無く
（地図は既存 Activity の内側。`workflow.patched` 不要）、workflow と Activity 契約は地図の結線を import しない。
ON は `AVP_QUALITY_GATE_ENFORCE=true` でなければ worker が起動しない。
**機械検査**: `tests/unit/test_geo_p3_flag.py::test_geo_none_keeps_the_pre_geo_input_hash`
/ `tests/unit/test_geo_p3_flag.py::test_maps_cannot_be_enabled_while_the_quality_gate_is_not_enforced`
/ `tests/unit/test_render_activities_geo.py::test_flag_off_writes_no_geo_evidence_and_keeps_the_same_identity`
/ `tests/unit/test_production_plan_geo.py::test_flag_off_does_not_look_at_geo_at_all`
/ `tests/architecture/test_geo_boundaries.py::test_workflows_and_activity_contracts_do_not_import_the_wiring`
/ `tests/architecture/test_geo_boundaries.py::test_the_setting_is_read_only_where_workers_are_assembled`
/ `tests/unit/test_geo_maps_flag_off.py::test_render_identity_and_filtergraph_are_byte_identical_to_before`

### INV-144 地図の結線はネットワーク・有料 provider に触れず、子プロセスはクリップの ffmpeg だけ
`infrastructure/geo/` は provider・httpx・socket 等を import せず、子プロセスを起こすのは `clip.py`（render worker が
検証した固定版 ffmpeg）だけ。gazetteer・下地・根拠はローカルの JSON で、実行時に外へ出ない。import 検出は相対 import・
`from pkg import submodule`・動的 import も見る（Phase 2 報告 §4-8 の抜けを塞いだ）。
**機械検査**: `tests/architecture/test_geo_boundaries.py::test_the_wiring_imports_no_paid_provider_or_network_module`
/ `tests/architecture/test_geo_boundaries.py::test_only_the_clip_encoder_starts_a_process`
/ `tests/architecture/test_geo_boundaries.py::test_the_detector_sees_from_package_import_submodule`
/ `tests/architecture/test_geo_boundaries.py::test_the_detector_resolves_relative_imports`
/ `tests/architecture/test_geo_boundaries.py::test_the_detector_sees_dynamic_imports`
/ `tests/architecture/test_geo_boundaries.py::test_production_billing_and_entrypoints_do_not_import_geo`

### INV-145 地図の文字は固定したフォントで描かれ、描けない字は使われない
Render は sha256 検証済みの `render_font_path`（CJK）のバイト列で描き、フォントの識別子（`font-sha256:…`）は記録に、
フォントの sha256 は render identity に入る。フォントに無い字（豆腐）は見た目の検査の BLOCK（`FONT_MISSING_GLYPHS`）で
その地図は使われない。下地を使う地図は出典の注記を必ず描く（現代の海岸線を当時の地形に見せない）。
**機械検査**: `tests/unit/test_geo_p3_record_and_quality.py::test_a_map_whose_text_cannot_be_drawn_is_not_applied`
/ `tests/unit/test_geo_p3_infra.py::test_japanese_labels_pass_with_the_pinned_cjk_font_and_the_font_is_in_the_identity`
/ `tests/unit/test_geo_p3_infra.py::test_the_base_caption_is_part_of_the_layout`
/ `tests/unit/test_geo_p3_infra.py::test_a_basemap_changes_the_image_and_its_identity_only_when_given`

## AK. BGM・地図の品質検査の結線（ADR-0086。INV-146〜148。既存 3 flag の背後、新しい flag なし）

### INV-146 使用宣言は ENFORCE が ON のときだけ、final_video の行を記録した後に書かれ、書けなくても描画は成功する
`AVP_QUALITY_GATE_ENFORCE` が OFF（既定）なら writer が渡されず、宣言も証跡の追加も DB / MinIO への書き込みは 1 つも増えない。
ON では BGM（`applied` なら used・証跡の sha256、それ以外は not used）と MAP（地図の指定があれば拒否された地図も used、
無ければ not used）を現行の対象 hash・入力 hash に結び付けて書く。書けなかった宣言は型名だけをログに出し、描画は失敗しない
（宣言が無ければ gate は REVIEW）。発話中の声と BGM の差は BGM が適用された描画だけが測り、測れなければ `speech=None`
（REVIEW）。
**機械検査**: `tests/unit/test_render_usage_declarations.py::test_without_a_writer_nothing_is_declared`
/ `tests/unit/test_render_usage_declarations.py::test_a_map_is_declared_used_even_when_it_was_refused`
/ `tests/unit/test_render_usage_declarations.py::test_a_failing_declaration_does_not_fail_the_render`
/ `tests/unit/test_render_usage_declarations.py::test_a_unmeasurable_margin_keeps_the_render_and_leaves_the_evidence_unmeasured`
/ `tests/unit/test_render_usage_declarations.py::test_no_speech_probe_or_no_bgm_means_no_measurement`

### INV-147 BGM・地図の検査は、宣言と結び付いた証跡を読んだ producer だけが記録し、読めなければ記録せず REVIEW
証跡の key は（媒体 sha256, 入力 hash）から導く（`bgm_evidence_key` / `geo_evidence_key`）。producer は純関数を包むだけで、
証跡が無い・読めない・別の動画のもの・カタログが無いは例外（= 記録されない = REVIEW、PASS に倒れない）。ライセンスが
描画後に失効、発話余裕が足りない、地図が BLOCK は BLOCK。再描画で別 hash になった動画は古い宣言・記録を引き継がない。
`bgm_enabled` / `geo_maps_enabled` の読み手は増えない。
**機械検査**: `tests/unit/test_quality_wiring_gate.py::test_with_everything_on_and_fresh_records_the_episode_is_admitted`
/ `tests/unit/test_quality_wiring_gate.py::test_bgm_used_but_its_check_cannot_be_recorded_is_review`
/ `tests/unit/test_quality_wiring_gate.py::test_a_refused_map_stops_the_upload`
/ `tests/unit/test_quality_wiring_gate.py::test_a_re_rendered_video_does_not_inherit_the_old_declarations_or_records`
/ `tests/unit/test_quality_wiring_gate.py::test_a_license_revoked_after_rendering_is_blocked`
/ `tests/unit/test_review_p3_unwired_quality.py::test_review_production_gate_has_bgm_and_map_producers`
/ `tests/architecture/test_geo_boundaries.py::test_the_setting_is_read_only_where_workers_are_assembled`
/ `tests/architecture/test_bgm_render_boundaries.py::test_the_setting_key_is_read_only_where_the_worker_is_assembled`

### INV-148 gate が lint する説明欄と upload が送る説明欄は、BGM クレジットを含めて同一の読み口から作られる
両方が `BgmCreditSource.credit_lines` の結果を `build_upload_metadata(credits=...)` に渡す。説明欄の構成 flag が OFF なら
読まない（従来と同一）。BGM が無ければ `()`、証跡があるのに読めなければ例外（黙って落とさない）。
**機械検査**: `tests/unit/test_quality_wiring_gate.py::test_the_linted_description_equals_the_uploaded_one`
/ `tests/unit/test_quality_wiring_gate.py::test_credit_lines_are_not_read_while_the_description_flag_is_off`
/ `tests/unit/test_quality_wiring_gate.py::test_credit_lines_come_from_the_evidence_when_the_flag_is_on`
/ `tests/unit/test_quality_wiring_gate.py::test_an_unreadable_evidence_is_an_error_not_an_empty_credit`

### 補足（INV-126 の拡張。ADR-0086 §6）追記専用は DELETE / TRUNCATE も拒否し、承認は同じ episode の報告にしか結べない
migration 0019（未適用のため編集）が 4 表に DELETE / TRUNCATE 拒否のトリガを足し、episode への FK は RESTRICT、
承認の報告への複合 FK は `episode_id` を含む。
**機械検査**: `tests/contract/test_migration_0019_quality.py::test_every_table_rejects_delete_and_truncate_too`
/ `tests/contract/test_migration_0019_quality.py::test_episode_foreign_keys_restrict_and_the_approval_fk_includes_the_episode`
/ `tests/integration/test_quality_records_postgres.py::test_delete_and_truncate_are_refused_on_every_table`
/ `tests/integration/test_quality_records_postgres.py::test_an_approval_cannot_point_at_another_episodes_report`
