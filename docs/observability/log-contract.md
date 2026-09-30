# ログ契約（schema_version 1）

ADR-0040。語彙・型・上限の**唯一の宣言元**は [`contracts/logging.py`](../../contracts/logging.py)。
この文書はその意味と、現行モデルとの対応を書く。OpenSearch の mapping（`deploy/logging/opensearch/`）
との一致は contract test で検査する（mapping を追加するコミットで名指しする）。

> **ログは検索用の副本である。** 業務状態・課金・冪等性は PostgreSQL、Workflow の実行履歴は
> Temporal が正（INV-7/INV-8）。ログ検索の結果で課金済みかを判断したり、再実行・再開・投稿を
> 決めたりしない（INV-38）。「ログが無い」は未実行・収集停止・バッファ待ちを区別できない。

## 1. 形式

- stdout へ **1イベント＝1行の JSON**（UTF-8、改行は JSON 文字列内でエスケープ）。stderr は使わない
  （Python の未捕捉例外も同じ整形器を通す）。
- 1行（改行を除く）は `EVENT_MAX_BYTES`（12,288 bytes）以下。Docker json-file は 16KiB を超える行を
  partial に分割するため、外側の包装とエスケープ増分を見込んで下回らせる。超えるときは
  `attributes` → `exception_stack` → `response_excerpt` → `message` の順に削り `truncated=true`。
- `@timestamp` は発生時刻（UTC、ISO 8601、ミリ秒、`Z`）。Workflow 内は SDK の logging 経由で
  handler 側の時刻を使う（Workflow のコードから時刻を読まない）。
- トップレベルは `LOG_FIELDS` のキーだけ。それ以外は安全化して `attributes`（検索対象外の object）へ。
  自由な provider JSON をトップレベルへ展開しない。

## 2. フィールド

型は OpenSearch の mapping 型。`always` は全イベントで必須、他は該当する処理だけで付ける。
**未取得の値は付けない**（空文字・推測値・配列番号で埋めない）。

| フィールド | 型 | 付く場面・意味 |
|---|---|---|
| `@timestamp` | date | 発生時刻 UTC（always） |
| `schema_version` | integer | `LOG_SCHEMA_VERSION`（always） |
| `event_id` | keyword | 1回の発行の ID（always）。§4 |
| `event_name` | keyword | `EventName` の固定値（always）。第三者 logger は `log.record` |
| `level` | keyword | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`（always） |
| `message` | text | 人向けの説明（安全化済み、≤2048B）（always） |
| `service_name` | keyword | `AVP_SERVICE_NAME`（compose のサービス名と同じ値。例 `production-image-worker`）（always） |
| `environment` | keyword | `AVP_ENVIRONMENT`（`prod`/`dev`/`test`）。未設定は `unknown`（always） |
| `git_sha` | keyword | イメージに焼いた `AVP_GIT_REVISION`。不明は `unknown`（always） |
| `logger` | keyword | Python logger 名（always） |
| `episode_id` | keyword | `episodes.id`。Activity/Workflow の入力・API の path から。推測しない |
| `scene_id` | keyword | storyboard の永続シーン ID（`sb6` 等。`artifact_metadata.scene_id` / `provider_reservations.scene_id` と同じ値）。配列番号で代用しない |
| `scene_revision` | integer | そのシーンの現行代替映像案（`scene_visual_override`）の `revision`。0 = storyboard の原案。その処理が revision を知っている時だけ |
| `stage` | keyword | `LogStage`（工程）。Episode の状態値ではない |
| `job_id` | keyword | `jobs.id` |
| `research_request_id` | keyword | `research_requests.id` |
| `workflow_id` / `run_id` / `workflow_type` | keyword | Temporal の `workflow.info()` / `activity.info()` |
| `activity_id` / `activity_type` / `task_queue` | keyword | Temporal の `activity.info()` |
| `activity_attempt` | integer | `activity.info().attempt`（Temporal の Activity 再試行。1始まり） |
| `request_id` | keyword | API の1リクエスト（受信 `X-Request-ID` が安全な形式ならそれ、無ければ生成） |
| `correlation_id` | keyword | 複数リクエスト・workflow をまたぐ照合用（現状は API→workflow 起動で `request_id` を引き継ぐ場合のみ） |
| `trace_id` / `span_id` | keyword | 将来の Traces 用（W3C 形式）。**実在する値がある時だけ**。現状は発行しない |
| `http_method` / `http_route` | keyword | API のメソッドと route template（`/episodes/{episode_id}/resume`。実 path ではない） |
| `provider` | keyword | `contracts.states.ProviderCall` の値（`fal_image` 等）。台帳の `provider` 列と同じ |
| `provider_operation` | keyword | `ProviderOperation`（submit/status/result/download/…） |
| `provider_endpoint` | keyword | provider のモデル・endpoint 名（`fal-ai/bytedance/seedream/...`）。URL ではない |
| `provider_request_id` | keyword | provider が返したジョブ ID（fal の `request_id`）。**受け取った後だけ** |
| `provider_attempt` | integer | 同じ入力に対する台帳のラウンド（`provider_reservations.round`）＝有料 submit の通番。Activity 再試行（`activity_attempt`）とも poll の回数とも別 |
| `reservation_id` | keyword | `provider_reservations.id`。**予約の commit 後だけ** |
| `reservation_status` | keyword | commit 済みの予約状態（`ReservationStatus`） |
| `artifact_id` / `artifact_type` | keyword | `artifact_metadata.id` / `ArtifactType` |
| `input_hash` | keyword | 入力指紋（sha256 hex。秘密ではない） |
| `outcome` | keyword | `Outcome` |
| `http_status` | integer | **観測した** HTTP status |
| `duration_ms` | double | 処理時間 |
| `error_type` | keyword | 例外クラス名（観測） |
| `error_code` | keyword | provider のエラー種別（fal の `detail[].type` 等。観測。複数は最初の1つ、全部は `attributes.error_codes`） |
| `error_category` | keyword | `ErrorCategory`（**推定**。ログ専用で制御に使わない） |
| `classification_basis` | keyword | `ClassificationBasis`（推定の根拠） |
| `failure_class` | keyword | 既存の `FailureClass`（`transient`/`retryable`/`needs_input`/`permanent`）。既存コードが決めた値をそのまま |
| `retryable` | boolean | 既存コードがこの失敗を自動再試行の対象にするか（観測した制御の事実） |
| `error_message` | text | 安全化済みの例外メッセージ（≤1024B） |
| `response_excerpt` | text | provider 応答から**許可した項目だけ**を抜き出した安全化済み JSON（≤4096B） |
| `exception_stack` | text | 安全化済み stack（≤8192B。末尾側を残す） |
| `redaction_applied` | boolean | このイベントで秘密の置換・URL の縮約をしたか |
| `response_truncated` / `stack_truncated` / `truncated` | boolean | excerpt / stack / イベント全体を切り詰めたか |
| `attributes` | object（enabled:false） | 安全化済みの補助情報。検索対象外（`_source` には残る） |
| `ingested_at` | date | OpenSearch の ingest pipeline が付ける取り込み時刻（Collector 側） |
| `log_source` | keyword | `app_json` / `unstructured`（Collector 側） |
| `stream` / `container_name` / `compose_service` / `compose_project` | keyword | Docker json-file の `attrs`（Collector 側。Docker socket は使わない） |
| `collector_errors` | keyword | Collector が型検査で直した・外したフィールド名（Collector 側） |

### 2.1 観測事実と推定

- 観測: `http_status`、`error_type`、`error_code`、`response_excerpt`、`failure_class`、`retryable`。
- 推定: `error_category` と、その根拠 `classification_basis`。
- **HTTP 403 だけ**なら `error_category=access_denied` / `classification_basis=http_status_only`。
  既存の例外メッセージにある "(credentials)" は既存コードのラベルで、ログ契約は credentials と断定しない。
  401 は `auth_rejected`。本文に根拠（provider のエラー種別）があれば `provider_error_type`。
- 422 は本文の `detail[].type` から: `content_policy_violation` → `content_policy`、
  `file_download_error` → `input_unreachable`、`REJECTED_ERROR_TYPES` → `input_validation`、
  それ以外 → `unknown`（既存 `fal_queue.rejection_category` と同じ規則を**参照**し、写さない）。
- 分類の追加・変更は既存の再試行・422 fallback・needs_input・課金の制御を変えない。

## 3. 現行モデルとの対応

| 要求された項目 | 現行モデル | ログのフィールド |
|---|---|---|
| episode_id | `episodes.id` | `episode_id` |
| 永続 scene_id | `artifact_metadata.scene_id` / `provider_reservations.scene_id` / `provider_rejections.scene_id`（storyboard のシーン ID） | `scene_id` |
| scene_revision | `scene_visual_override` Artifact の `revision`（ADR-0035）。Artifact の `version` とは別 | `scene_revision` |
| supersede | `artifact_metadata.superseded_at`（現行世代の交代） | `artifact.superseded` イベント + `artifact_id` |
| stage | `JobType` / 各 workflow（工程） | `stage`（`LogStage`） |
| workflow_id / run_id / activity_id / task_queue / activity_attempt | Temporal `workflow.info()` / `activity.info()` | 同名 |
| provider_attempt | `provider_reservations.round`（台帳ラウンド。`PaidJobSpec.round` は run ごとの試行番号でログ用、台帳ではない） | `provider_attempt`（台帳ラウンド）。`PaidJobSpec.round` は `attributes.run_attempt` |
| request_id / correlation_id | 既存なし（新設） | 同名 |
| provider | `ProviderCall`（台帳 `provider` 列） | `provider` |
| provider_operation | `FalQueueClient.submit/status/result/download`、`fal_storage`、CLI 起動、YouTube | `provider_operation` |
| provider_request_id | `provider_reservations.provider_job_ref` 内の `request_id`（fal）/ `provider_result_ref`（YouTube video id） | `provider_request_id` |
| paid_job_id | **該当する永続 ID は無い**（`PaidJobRunner` はクラス。台帳の行が単位） | 使わない。`reservation_id` と `job_id` で照合 |
| reservation_id | `provider_reservations.id` | `reservation_id` |
| 課金確定 | `provider_reservations.status`（`reserved`→`spent`/`abandoned`）と `dispatched_at` | `reservation.*` イベント（commit 後のみ）+ `reservation_status` |
| 403 の記録 | `provider_auth_incidents`（ADR-0030） | `provider.call.failed` + `provider.auth_incident.recorded` |
| 422 / file_download_error | `provider_rejections`（`category`・`types`・`http_status`） | `scene.rejected` + `error_code` / `error_category` |
| 既存の fallback | 代替映像案（`scene_alternative`、ADR-0035）、入力の上げ直し（INV-35） | `scene.alternative.*` / `scene.input_refetch` |
| resume | `POST /episodes/{id}/resume`（ADR-0032） | `episode.resume.*` |
| 成功済み sceneの再利用 | `PaidJobRunner.submit` の `Reused`（ADR-0033 の完全性検証後） | `artifact.reused` |
| Upload の既存動画再利用 | 予約の `provider_result_ref`・受領 Artifact（INV-14） | `upload.reused_existing` |
| 運用異常 | `operational_anomalies`（avp.anomaly logger） | `anomaly.recorded` |

## 4. event_id

- Activity・API・起動処理: 発行時に `uuid4().hex`。**1回の発行＝1つの ID**。同じ行が Collector から
  再送されても行の中の ID は変わらない。Activity の再試行・provider の再送は別の発行なので別 ID。
- Workflow: 決定的に導く。`uuid5(AVP_LOG_NAMESPACE, f"{run_id}:{history_length}:{seq}:{event_name}")`
  （`seq` は同じ workflow task 内の発行順）。`workflow.uuid4()` / `workflow.random()` は**使わない**
  （乱数列を消費すると既存 workflow の以後の乱数が変わり、稼働中の履歴と非決定になる）。
- OpenSearch の文書 `_id` に `event_id` を使う。重複抑制は**同じ index 内だけ**。rollover をまたぐ
  再送は別文書になり得るので、exactly-once とは言わない。検索側は `event_id` で重複を除ける。

## 5. 文脈の引き回し

- `contextvars` で持つ。束縛は `with log_context(...)`（抜けるときに token で必ず戻す）。
- Activity: Worker の interceptor が `activity.info()` と入力の `episode_id`/`scene_id` 属性（あれば）を
  束縛し、終了時に解除する。Activity ごとに別 task なので並列 Episode 間で混ざらない。
  同期 Activity（thread 実行）では `contextvars.copy_context()` で引き継ぐ。
- API: middleware が `request_id`・route・path の `episode_id` を束縛し、応答後に解除する。
- Workflow: contextvars を使わない。`workflow.info()` から毎回取る。発行は replay 中は抑止する
  （`workflow.unsafe.is_replaying()`）。Workflow のコードに OS 時刻・通常乱数・任意 UUID・I/O を足さない。

## 6. レベルと量

- 既定 INFO。poll（status の1回）と heartbeat は DEBUG。poll は**状態が変わった時だけ**
  `provider.job.state_changed` を INFO。`activity.started` と `provider.call.started` は DEBUG。
- 1 Episode（約10シーン）の INFO 以上は数百件程度を目安にする（§8 の容量試算の前提）。

## 7. 安全化（stdout に出す前）

整形器（`infrastructure/logging`）で行う。Collector の Lua フィルタは追加の防御。

1. トップレベルは許可リスト（`LOG_FIELD_NAMES`）。それ以外は `attributes` へ。
2. キー名が秘密を示すもの（`authorization`、`cookie`、`set-cookie`、`token`、`secret`、`password`、
   `passwd`、`api_key`/`apikey`/`key`（単独）、`credential`、`private_key`、`dsn`、`database_url`、
   `connection_string`、`signature`、`x-amz-*`、`fal_key`、`refresh_token`、`client_secret`）の値は
   `[REDACTED]`。
3. 値のパターン: `Authorization` 形式（`Bearer …`/`Basic …`/`Key …`）、fal key 形式、JWT、
   `-----BEGIN … PRIVATE KEY-----` ブロック、userinfo 付き URL/DSN、Google OAuth token（`ya29.`・`1//`）、
   `sk-…` 形式を `[REDACTED]` に置換。
4. URL: userinfo・query・fragment を落とす。path は許可した API host（`queue.fal.run`、
   `rest.alpha.fal.ai`、`www.googleapis.com`、`youtube.googleapis.com`、`oauth2.googleapis.com`）
   だけ残し、それ以外（`fal.media`・`v3.fal.media` 等の署名/capability URL を含む）は
   `scheme://host/…#sha256:<先頭12桁>` に縮約。
5. provider 応答は**許可した項目だけ**を抜き出す（fal: `detail[].type`/`loc`/`msg`（安全化・切詰）/
   `ctx.reason`、HTTP ヘッダ: `x-fal-retryable`、`x-fal-request-id`、`retry-after`、`content-type`）。
   応答全文、prompt 全文、base64・画像・音声・動画のバイト列は入れない（長い base64 様の値は除去）。
6. 例外文字列・stack・第三者 logger（`httpx`・`temporalio`・`uvicorn` 等）の記録も同じ処理を通す。
   環境変数や `Settings` の dump を出さない。
7. 置換・縮約をしたら `redaction_applied=true`、切り詰めたら該当の `*_truncated=true`。
8. 整形・発行の失敗は業務に伝播させない（`logging.raiseExceptions=False` 相当・handler 内で握る）。
   失敗した記録は最小の固定形（`event_name=log.record`、`message="log formatting failed"`、
   `error_type`）で出し直す。

## 8. イベント一覧

`contracts.logging.EventName` の各値。発行位置は実装（ADR-0040 §実装の境界）に従う。

| event_name | 主な場面 |
|---|---|
| `log.record` | event_name を持たない記録（既存・第三者 logger） |
| `service.started` / `service.stopped` / `service.start_failed` | worker_entry・API 起動終了 |
| `api.request.completed` | API の1リクエスト（health は DEBUG） |
| `schedule.slot.acquired` / `schedule.slot.skipped` | 日次枠の取得・スキップと理由 |
| `stage.started` / `stage.succeeded` / `stage.failed` / `stage.blocked` / `stage.skipped` | 各工程 workflow（replay 安全） |
| `activity.started`(DEBUG) / `activity.succeeded` / `activity.failed` | interceptor。`duration_ms`・失敗分類 |
| `provider.call.started`(DEBUG) / `provider.call.succeeded` / `provider.call.failed` | 外部呼び出し1回。`provider_operation` で区別 |
| `provider.job.state_changed` | poll で状態が変わった時だけ |
| `provider.auth_incident.recorded` / `provider.call.suppressed` | ADR-0030 の記録・抑止 |
| `reservation.reserved` / `reservation.dispatched` / `reservation.job_ref_recorded` / `reservation.spent` / `reservation.resumed` / `reservation.blocked` | 予約台帳。**commit 後だけ** |
| `artifact.stored` / `artifact.reused` / `artifact.reuse_rejected` / `artifact.superseded` | 成果物 |
| `scene.rejected` / `scene.input_refetch` / `scene.alternative.started` / `scene.alternative.result` | 403/422/file_download_error と既存 fallback |
| `episode.resume.requested` / `episode.resume.rejected` / `episode.resume.started` | 統一再開 |
| `render.validation.passed` / `render.validation.failed` | Render の尺・音声検証 |
| `upload.started` / `upload.succeeded` / `upload.reused_existing` / `upload.skipped` / `upload.failed` | Upload |
| `research.request.started` / `research.request.finished` | Research |
| `anomaly.recorded` | 運用異常の記録 |
