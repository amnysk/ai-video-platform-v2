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
EpisodePipeline・Daily の履歴を、今のコードの Replayer で replay。非決定にならず1件も発行しない）
/ `tests/unit/test_log_old_history_replay.py::test_the_old_histories_cover_every_stage_workflow`
/ `tests/architecture/test_logging_boundaries.py::test_workflow_event_names_and_stages_are_contract_vocabulary`
/ `tests/unit/test_pipeline_workflows.py`（既存の履歴 fixture の replay がログ発行を足した後も通る）
