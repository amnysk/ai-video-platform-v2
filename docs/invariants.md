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
final_video の sha256 または input_hash が**今の**ものと不一致、未来日付は、すべて拒否し理由コードを返す。
許可は検証済みの PASS と WARN（報告つき）だけ。（設計 v3 T22）
**機械検査**: `tests/unit/test_quality_upload_decision.py`（全拒否経路と許可経路）
/ `tests/contract/test_quality_contract.py::test_malformed_reports_are_rejected_with_a_reason`
/ `tests/contract/test_quality_contract.py::test_non_report_input_is_unparsable`
/ `tests/unit/test_quality_upload_decision.py::test_no_input_can_make_evaluate_raise`（総関数・fail-closed）
/ `tests/unit/test_quality_upload_decision.py::test_deeply_nested_json_is_refused_not_raised`
/ `tests/unit/test_quality_upload_decision.py::test_naive_generated_at_is_refused_not_raised`
/ `tests/unit/test_quality_upload_decision.py::test_checks_must_be_a_tuple_of_quality_checks`
/ `tests/contract/test_quality_contract.py::test_error_messages_never_echo_untrusted_values`（値を例外文に写さない）
（判定は純粋関数の範囲。`upload_gate` への配線は INV-104）

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

### INV-104 `upload_gate` は有効な品質レポートが無ければ投稿を拒否する（**配線は未実装**）
計画: `upload_gate._refusal()` を拡張し（新しい stage は作らない）、final_video の sha256 と品質レポートを読んで
`evaluate_for_upload` の拒否理由を `UploadGateResult.reason` に載せる。**現行の `upload_gate` は品質レポートを
見ない**（本ブランチは Activity / workflow を変更しない。ADR-0073 §6）。保存先・遷移表の変更は別 ADR で先に承認する。
**機械検査**: 未検査（配線の承認・実装まで。INV-101 の純粋な判定だけが検査済み）


## AC. 投稿の照合と説明欄の構成（ADR-0075。INV-109〜112）

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
