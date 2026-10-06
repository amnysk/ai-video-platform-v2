# ログ契約（schema_version 1）

ADR-0040。語彙・型・上限・設定キーの**唯一の宣言元**は
[`contracts/log_contract.py`](../../contracts/log_contract.py)。この文書はその意味と、現行モデルとの
対応を書く。OpenSearch の mapping と Collector の型表は `contracts/log_contract.py` から**生成**し、
生成物との一致を contract test で検査する（手書きの写しを作らない）。

> **ログは検索用の副本である。** 業務状態・課金・冪等性は PostgreSQL、Workflow の実行履歴は
> Temporal が正（INV-7/INV-8）。ログ検索の結果を課金済みか・再実行・再開・投稿の**自動判断**に
> 使わない（INV-38）。人手の照合でログを手がかりにするのはよいが、確定は DB・Temporal・provider 側の
> 記録で行う。「ログが無い」は未実行・収集停止・バッファ待ち・rotation による欠損を区別できない。

## 1. 形式

- stdout へ **1イベント＝1行の JSON**（UTF-8、`ensure_ascii=False`、改行は JSON 文字列内でエスケープ）。
  アプリは stderr に書かない（未捕捉例外・`warnings`・Temporal Core のログも同じ整形器を通す。§7.6）。
- 1行（改行を除く）は `EVENT_MAX_BYTES`（12,288 bytes）以下。Docker json-file はアプリが書いた生の
  行を 16,384 bytes で partial に分割するので、それを下回らせる。超えるときは `attributes` →
  `exception_stack` → `response_excerpt` → `message` の順に削り `truncated=true`。
- `@timestamp` は発生時刻（UTC、ISO 8601、ミリ秒、`Z`）。LogRecord の生成時刻を使う。Workflow の
  コードは時刻を読まない（handler 側で付く）。
- トップレベルは `LOG_FIELDS` のキーだけ。それ以外は安全化して `attributes`（検索対象外）へ。
  自由な provider JSON をトップレベルへ展開しない。
- keyword の値は `KEYWORD_MAX_CHARS`（512字）で切り詰め、`truncated=true`。
- `AVP_LOG_FORMAT=text` で従来の `basicConfig` 形式に戻せる（rollback 用。安全化は text でも行う）。

## 2. フィールド

型は OpenSearch の mapping 型。`always` は全イベントで必須、他は該当する処理だけで付ける。
**未取得の値は付けない**（空文字・推測値・配列番号で埋めない）。

| フィールド | 型 | 付く場面・意味 |
|---|---|---|
| `@timestamp` | date | 発生時刻 UTC（always） |
| `schema_version` | integer | `LOG_SCHEMA_VERSION`（always） |
| `event_id` | keyword | 1回の発行の ID（always）。§4 |
| `event_name` | keyword | `EventName` の固定値（always）。event_name を持たない記録（既存・第三者 logger）は `log.record` |
| `level` | keyword | `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`（always） |
| `message` | text | 人向けの説明（安全化済み、≤2048B）（always）。既存のログ文言は変えない |
| `service_name` | keyword | `AVP_SERVICE_NAME`（compose のサービス名。例 `production-image-worker`）。未設定は `unknown`（always） |
| `environment` | keyword | `AVP_ENVIRONMENT`（`Environment`: `prod`/`dev`/`test`）。未設定は `unknown`（always） |
| `git_sha` | keyword | イメージに焼いた `AVP_GIT_REVISION`。不明は `unknown`（always） |
| `logger` | keyword | Python logger 名（always） |
| `episode_id` | keyword | `episodes.id`。入力・API の path から。推測しない |
| `scene_id` | keyword | その処理の **`artifact_metadata.scene_id` の名前空間**のシーン ID。画像・動画・拒否・代替案は storyboard のシーン ID（`sb6`）、音声は台本のシーン ID（音声 Artifact の scene_id と同じ）。音声の対応 storyboard シーンは `attributes.storyboard_scene_ids`。配列番号で代用しない |
| `scene_revision` | integer | そのシーンの現行代替映像案（`scene_visual_override`）の `revision`。0 = storyboard の原案。その処理が revision を知っている時だけ |
| `storyboard_artifact_id` | keyword | シーンが属する storyboard Artifact の id。storyboard の作り直しで `sb6` が再利用されるので、世代をまたぐ照合に使う。その処理が知っている時だけ |
| `stage` | keyword | `LogStage`（工程）。Episode の状態値ではない |
| `job_id` | keyword | `jobs.id` |
| `research_request_id` / `research_call_id` | keyword | `research_requests.id` / `research_calls.id` |
| `workflow_id` / `run_id` / `workflow_type` | keyword | Temporal の workflow info（Activity では `temporal_activity` の `workflow_id`/`workflow_run_id`/`workflow_type`） |
| `activity_id` / `activity_type` / `task_queue` | keyword | Temporal の activity info |
| `activity_attempt` | integer | activity info の `attempt`（**Temporal の Activity 再試行**。1始まり） |
| `request_id` | keyword | **API の1リクエスト**（受信 `X-Request-ID` が `[A-Za-z0-9._-]{1,64}` ならそれ、無ければ生成）。Research の `request_id`（依頼 ID）はここに入れず `research_request_id` へ |
| `correlation_id` | keyword | 判断・介入の行が持つ `correlation_id`（API・command・agent run を跨いで結ぶ）。それ以外の処理では発行しない（resume は決定的な `workflow_id` で結合できる） |
| `trace_id` / `span_id` | keyword | 将来の Traces 用（W3C 形式）。**実在する値がある時だけ**。v1 では発行しない |
| `http_method` / `http_route` | keyword | API のメソッドと route template（`/episodes/{episode_id}/resume`。実 path・query ではない） |
| `provider` | keyword | 予約台帳の呼び出しは `ProviderCall` の値（`fal_image` 等、台帳の `provider` 列と同じ）。台帳外の呼び出しは `ProviderLabel`（`fal_storage`、`youtube_analytics`、`youtube_oauth`、`piper`、`openmontage`、`research_search`、`research_fetch`）|
| `provider_operation` | keyword | `ProviderOperation`（submit/status/result/download/input_upload/storage_token/…） |
| `provider_endpoint` | keyword | provider のモデル・endpoint 名（`fal-ai/bytedance/seedream/...`）。URL ではない |
| `provider_request_id` | keyword | provider が返したジョブ ID（fal の `request_id`）。**受け取った後だけ**。YouTube の video id は入れず `attributes.video_id` |
| `provider_attempt` | integer | 同じ `input_hash` に対する台帳のラウンド（`provider_reservations.round`）＝有料 submit の通番。一意にするには `(input_hash, provider_attempt)` の組で見る。Activity 再試行（`activity_attempt`）とも poll の回数とも別 |
| `reservation_id` | keyword | `provider_reservations.id`。**予約の commit 後だけ** |
| `reservation_status` | keyword | commit 済みの予約状態（`ReservationStatus`） |
| `artifact_id` / `artifact_type` | keyword | `artifact_metadata.id` / `ArtifactType` |
| `input_hash` | keyword | 入力指紋（sha256 hex。秘密ではない） |
| `event_seq` | long | 判断・介入イベントの Episode 内通番（`episode_events.seq`）。投影・再投影のときだけ。`(episode_id, event_seq)` で正本の1行に戻れる |
| `manifest_id` | keyword | 採用 manifest（`artifact_manifests.id`）。その処理が manifest を知っている時だけ |
| `agent_run_id` | keyword | 役割実行の記録（`agent_runs.id`）。prompt・応答の本文は載せず、MinIO の参照は DB 側にだけ持つ |
| `decision_id` | keyword | 判断記録（`decision_records.id`） |
| `command_id` | keyword | 介入 command（`intervention_commands.id`。クライアントが決めた冪等キー） |
| `actor_type` | keyword | 変更・判断の主体（`contracts.intervention.ActorType`: `human` / `ai_assistant` / `agent` / `system`）。actor の個人 ID は `attributes.actor_id` |
| `model` | keyword | agent の model 識別子（設定値。CLI が実際に使ったモデルとは限らない／ADR-0041） |
| `prompt_version` | keyword | agent の prompt の版。prompt 本文ではない |
| `usage_input` / `usage_output` | long | agent の入力・出力の token 数（設計 §15 の token_usage）。**取得できない時は付けない**（0 で埋めない）。名前に `token` を含めないのは §7.2 の鍵名による伏せ字に当てないため |
| `estimated_cost_usd` / `actual_cost_usd` | double | 見積り・実費（USD）。課金の正本は予約台帳（`provider_reservations`）。ログの値で課金を判断しない（INV-38） |
| `outcome` | keyword | `Outcome` |
| `http_status` | integer | **観測した** HTTP status |
| `duration_ms` | double | 処理時間 |
| `error_type` | keyword | 観測した失敗の型名。Python 例外はクラス名、Temporal の `ApplicationError` はその `type`（ドメインの例外名） |
| `error_code` | keyword（配列可） | provider のエラー種別（fal の `detail[].type` と body 直下の `error_type`、ヘッダ `x-fal-error-type`）。**全部**を配列で（観測） |
| `error_category` | keyword | `ErrorCategory`（**推定**。ログ専用で制御に使わない） |
| `classification_basis` | keyword | `ClassificationBasis`（推定の根拠） |
| `failure_class` | keyword | 既存の `FailureClass`（`failure_class_from_type_name` 等、既存コードが決めた値をそのまま） |
| `retryable` | boolean | **Temporal がこの Activity を再試行し得るか**（`non_retryable` でない）。workflow 側の判断（新ラウンド・needs_input）は `failure_class` と後続イベントで見る |
| `error_message` | text | 安全化済みの例外メッセージ（≤1024B） |
| `response_excerpt` | text | provider 応答から**許可した項目だけ**を抜き出した安全化済み JSON（≤4096B）。§7.5 |
| `exception_stack` | text | 安全化済み stack（≤8192B）。§7.7 |
| `redaction_applied` | boolean | このイベントで秘密の置換・URL の縮約をしたか |
| `response_truncated` / `stack_truncated` / `truncated` | boolean | excerpt / stack / その他（イベント全体・message・keyword）を切り詰めたか |
| `attributes` | object（enabled:false） | 安全化済みの補助情報（≤4096B）。検索対象外（`_source` には残る） |
| `ingested_at` | date | OpenSearch の ingest pipeline が付ける取り込み時刻（Collector 側） |
| `log_source` | keyword | `LogSource`（Collector 側） |
| `stream` / `container_name` / `compose_service` / `compose_project` / `host_name` | keyword | Docker json-file の `attrs` と Collector のホスト名（Collector 側。Docker socket は使わない） |
| `collector_errors` | keyword（配列） | Collector が型検査で `attributes.collector_moved` へ退避したフィールド名、`@timestamp_replaced`・`json_parse_failed`・`line_too_long`（`COLLECTOR_LINE_MAX_BYTES` を超えて切った行）等（Collector 側）|

### 2.0 infra（unstructured）系統のフィールド

`log_source=unstructured` の文書（JSON でない行・stderr・label の無いコンテナ）が持つのは
`contracts.log_contract.INFRA_FIELD_NAMES` だけ: Collector 側のフィールド全部（`ingested_at`・`log_source`・
`stream`・`container_name`・`compose_service`・`compose_project`・`collector_errors`・`host_name`）と、
Collector が行から作る `@timestamp`（Docker の時刻）・`message`（安全化し `UNSTRUCTURED_LINE_MAX_BYTES` で
切り詰めた行）・`truncated`・`redaction_applied`。infra の mapping はこの集合から生成し、Collector が書くキーが
その部分集合であることを integration test（`tests/integration/test_logging_collector_lua.py`）で固定する。

### 2.1 観測事実と推定

- 観測: `http_status`、`error_type`、`error_code`、`response_excerpt`、`failure_class`、`retryable`。
- 推定: `error_category` と、その根拠 `classification_basis`。
- **HTTP 403 だけ**なら `error_category=access_denied` / `classification_basis=http_status_only`。
  既存の例外メッセージにある "(credentials)" は既存コードのラベルで、ログ契約は credentials と断定しない。
  401 は `auth_rejected`（同じく `http_status_only`）。provider のエラー種別が根拠なら `provider_error_type`、
  `x-fal-retryable` 等のヘッダなら `provider_header`、例外の型だけなら `exception_type`。
- 422 等の本文の種別は既存 `fal_queue.rejection_category()` の結果（`RejectionCategory`）をそのまま
  `error_category` にする（規則を写さない）。
- 分類はログ専用。既存の再試行・422 fallback・needs_input・課金・例外の型を変えない。

## 3. 現行モデルとの対応

| 要求された項目 | 現行モデル | ログのフィールド |
|---|---|---|
| episode_id | `episodes.id` | `episode_id` |
| 永続 scene_id | `artifact_metadata.scene_id` / `provider_reservations.scene_id` / `provider_rejections.scene_id`（画像・動画は storyboard のシーン ID、音声は台本のシーン ID）| `scene_id`（+ `storyboard_artifact_id` で世代を区別） |
| scene_revision | `scene_visual_override` Artifact の `revision`（ADR-0035。`len(overrides)+1`、storyboard の世代をまたいで数える）。Artifact の `version` とは別 | `scene_revision` |
| supersede | `artifact_metadata.superseded_at`（`repositories._supersede_current`） | `artifact.superseded`（commit 後）+ `artifact_id` |
| stage | `JobType` / 各 workflow（工程） | `stage`（`LogStage`） |
| workflow_id / run_id / activity_id / task_queue / activity_attempt | Temporal の info（Activity の extra は `attempt`・`workflow_run_id`） | 同名 |
| provider_attempt | `provider_reservations.round`（台帳ラウンド）。`PaidJobSpec.round` は run ごとの試行番号で台帳ではない | `provider_attempt`。`PaidJobSpec.round` は `attributes.run_attempt` |
| request_id / correlation_id | 既存なし（新設）。Research の依頼 ID は別物 | `request_id`（API のみ）/ `research_request_id` |
| provider | `ProviderCall`（台帳 `provider` 列）＋台帳外の固定ラベル | `provider` |
| provider_operation | `FalQueueClient.submit/status/result/download`、`fal_storage`、CLI 起動、YouTube | `provider_operation` |
| provider_request_id | `provider_reservations.provider_job_ref` 内の `request_id`（fal） | `provider_request_id`（YouTube video id は `attributes.video_id`） |
| paid_job_id | **該当する永続 ID は無い**（`PaidJobRunner` はクラス。台帳の行が単位） | 使わない。`reservation_id` と `job_id` で照合 |
| reservation_id | `provider_reservations.id` | `reservation_id` |
| 課金確定 | `provider_reservations.status`（`reserved`→`spent`/`abandoned`）・`dispatched_at`・`reconciled_by` | `reservation.*`（commit 後のみ）+ `reservation_status`、`attributes.reconciled_by` |
| 403 の記録 | `provider_auth_incidents`（ADR-0030） | `provider.call.failed` + `provider.auth_incident.recorded` |
| 422 / file_download_error | `provider_rejections`（`category`・`types`・`http_status`） | `scene.rejected` + `error_code` / `error_category` |
| 既存の fallback | 代替映像案（`scene_alternative`、ADR-0035）、入力の上げ直し（INV-35） | `scene.alternative.*` / `scene.input_refetch` |
| resume | `POST /episodes/{id}/resume`（ADR-0032） | `episode.resume.*` |
| 成功済み scene の再利用 | `PaidJobRunner.submit` の `Reused`（ADR-0033 の完全性検証後） | `artifact.reused` |
| Upload の既存動画再利用 | 予約の `provider_result_ref`・受領 Artifact（INV-14） | `upload.reused_existing` |
| 運用異常 | `operational_anomalies`（`avp.anomaly` logger。`OPERATIONAL_ANOMALY anomaly=` の文言は維持） | `anomaly.recorded` |
| 判断・介入イベント（設計 §15） | `episode_events`（正本。`seq`・`event_name`・`refs`・`command_id`・`actor_type`。ADR-0041） | `event_name`（同じ文字列）+ `event_seq` + `refs` の ID 群。§10 |
| artifact_revision | `artifact_metadata` の1行（`id` = revision_id） | `artifact_id`（新しいフィールドを作らない） |
| manifest_id / agent_run_id / decision_id / command_id | `artifact_manifests.id` / `agent_runs.id` / `decision_records.id` / `intervention_commands.id` | 同名 |
| actor_type | 新表の `actor_type` 列（`ActorType`） | `actor_type`（個人 ID は `attributes.actor_id`） |
| branch_id | **該当する永続 ID は無い**（別案は `change_sets.variant_group`） | 使わない。`attributes.variant_group` |
| tool_name / tool_call_id | `agent_runs.tool_invocations` の各要素が既存の `provider_reservations.id` / `research_calls.id` を参照 | `provider`（tool_name）・`reservation_id` / `research_call_id`（tool_call_id）。新しい ID を作らない |
| model / prompt_version / token_usage | `agent_runs.model` / `prompt_version` / `token_usage`（JSON。取得できなければ NULL） | `model` / `prompt_version` / `usage_input`・`usage_output` |
| estimated_cost / actual_cost | `agent_runs.estimated_cost_usd`・`provider_reservations.estimated_cost_usd`（実費の記録は無い） | `estimated_cost_usd` / `actual_cost_usd`（実費は記録がある時だけ） |
| duration / retry_count / outcome | 既存 | `duration_ms` / `activity_attempt`・`provider_attempt` / `outcome` |

## 4. event_id

- Activity・API・起動処理: 発行時に `uuid4().hex`。**1回の発行＝1つの ID**。Collector が同じ行を再送しても
  行の中の ID は変わらない。Activity の再試行・provider の再送は別の発行なので別 ID。
- 判断・介入イベントの投影（`episode_events` の1行）: `uuid5(PROJECTION_EVENT_NAMESPACE, f"{episode_id}:{seq}")`。
  live の投影と再投影で**同じ値**（§10）。発行側は `extra={"avp": {"projected_event_id": …}}` で渡し、整形器は
  32桁の小文字 hex の時だけ採用する（それ以外は無視して `uuid4`）。
- Workflow: Workflow のコードは ID を作らない。sandbox の外で import 済みの整形器が、Workflow スレッド内で
  `uuid5(AVP_WORKFLOW_EVENT_NAMESPACE, f"{workflow_id}:{run_id}:{history_length}:{seq}:{event_name}")`
  を導く（`history_length` は `workflow.info().get_current_history_length()`、`seq` は整形器が
  `(run_id, history_length)` ごとに数える発行順。上限つきの LRU で持ち、eviction で消えてよい）。
  `workflow.uuid4()` / `workflow.random()` は**使わない**（乱数列を消費すると既存 workflow の以後の乱数が
  変わり、稼働中の履歴と非決定になる）。
- replay 中の発行は SDK の判定（`is_replaying_history_events`）で抑止され、整形器まで届かない。
  workflow task が発行の後に失敗・timeout すると、次の task は `history_length` が変わるので**別の ID で
  再発行され得る**（workflow task の試行単位で at-least-once）。replay では重複しない（INV-40）。
- OpenSearch の文書 `_id` に `event_id` を使い、`create` で書く（既存 `_id` は 409＝成功扱い）。
  重複抑制は**同じ index 内だけ**。rollover をまたぐ再送は別文書になり得るので exactly-once とは言わない。
  検索側は `event_id` で重複を除ける。`event_id` が衝突すると後の文書が黙って捨てられるので、導出の
  入力が一意であることを unit test で固定する。

## 5. 文脈の引き回し

- `contextvars` で持つ。束縛は `with log_context(...)`（抜けるときに token で必ず戻す）。
- Activity: Worker の interceptor が activity info と、入力型ごとの**明示の対応表**で入力から
  `episode_id` / `scene_id` / `job_id` / `research_request_id`（Research 入力の `request_id` を写す）を
  束縛し、終了時（例外・cancel を含む）に解除する。属性名で汎用的に拾わない。Activity ごとに別 task
  なので並列 Episode 間で混ざらない（現行の Activity は全て async。同期 Activity は SDK が
  contextvars を引き継ぐ）。
- `PaidJobRunner` は prepare・submit・poll・download の周りで `provider`・`input_hash`・
  `provider_attempt`・`reservation_id`（commit 後）を束縛する。adapter（`FalQueueClient` 等）は
  自分が観測した `http_status`・`provider_operation`・`provider_request_id` だけを足す。
- API: pure ASGI middleware が `request_id` を束縛し、`api.request.completed` はルーティング後の
  `scope["route"]`・`path_params` から route template と `episode_id` を読む。handler 内のイベント
  （`episode.resume.*`）は handler が束縛する。
- Workflow: contextvars を使わない（worker 側の contextvars は Workflow から見えない）。Workflow の
  コードは `workflow.logger.<level>(msg, extra={"avp": {...}})` だけを使い、`infrastructure` を
  import しない。workflow info は SDK が付ける `temporal_workflow` から整形器が取る。
  Workflow のコードに OS 時刻・通常乱数・任意 UUID・I/O を足さない。query・update validator では発行しない。
- 発行のフィールドは必ず `extra={"avp": {...}}` の1キーに入れる（`message`・`name` 等 LogRecord の
  予約属性と衝突すると `makeRecord` が KeyError を投げ、`raiseExceptions=False` でも業務へ伝播する）。
  Workflow 以外の発行は `infrastructure.logging` のヘルパーを使い、ヘルパーは record の生成を含めて
  例外を握る。

## 6. レベルと量

- 既定 INFO。poll（status の1回）と heartbeat は DEBUG。`PaidJobRunner` の poll は pending→終端の
  変化と、Activity 試行ごとの最初の観測だけ `provider.job.state_changed` を INFO。
  `activity.started` と `provider.call.started` は DEBUG。
- 第三者 logger の既定: `httpx`・`httpcore` は WARNING（URL の query を出さない。既存 INV-20 対策を
  一か所へ寄せる）、`temporalio` は INFO、Core は WARN 以上。`sqlalchemy.engine`・`aiosqlite` は `AVP_LOG_LEVEL=DEBUG` でも WARNING に固定（DEBUG は行の値＝session URI 等を出す）。asyncio の未捕捉例外は `asyncio` logger 経由で root の整形器へ流れる。
- 1 Episode（約10シーン）の INFO 以上は数百件程度を目安にする（容量試算の前提、実測で更新）。

## 7. 安全化（stdout に出す前）

整形器（`infrastructure/logging`）で行う。Collector の Lua は追加の防御。**置換してから切り詰める**
（切り詰めで秘密のパターンが途中で切れて検出を逃れないように）。

1. トップレベルは許可リスト（`LOG_FIELD_NAMES`）。それ以外は `attributes` へ。
2. キー名: 小文字化し `-` を `_` に揃えて、次を**含む**キーの値は `[REDACTED]`:
   `authorization`、`cookie`、`token`、`secret`、`password`、`passwd`、`api_key`、`apikey`、
   `credential`、`private_key`、`dsn`、`database_url`、`connection_string`、`signature`、`x_amz_`、
   `fal_key`、`session_uri`、`upload_url`、`access_key`。次に**一致する**キーも同じ:
   `key`、`code`、`location`、`x_goog_upload_url`、`upload_id`。
3. 値のパターン: `Bearer …`/`Basic …`/`Key …`、fal key 形式、JWT、`-----BEGIN … PRIVATE KEY-----`
   ブロック、userinfo 付き URL・DSN（`postgresql+psycopg://user:pass@` のように scheme に `+` を含む形）、
   Google OAuth token（`ya29.`・`1//`）、`sk-…`、SQLAlchemy 例外の `[parameters: …]` 部分、
   長い base64 様の値（256字以上の `[A-Za-z0-9+/=_-]` の連続）。
4. URL: userinfo・query・fragment を落とす。path は許可した API host だけ残す。許可 host は**adapter の
   定数から導く**（`fal_queue.QUEUE_BASE_URL`、`fal_storage` の token endpoint、YouTube / Analytics /
   OAuth の base URL）。それ以外（`fal.media` 等の capability URL、YouTube の resumable session URI を
   含む）は `scheme://host/…#sha256:<先頭12桁>` に縮約。
5. provider 応答は**許可した項目だけ**。fal: 既に構造化済みの `ProviderRejection`（`domain/errors.py`）
   の `types`・`reason`（`ctx.extra_info.reason`）・`message`（安全化・切詰）と、body 直下の
   `error_type`、`detail[].loc`。ヘッダ: `x-fal-retryable`、`x-fal-error-type`、`x-fal-request-id`、
   `retry-after`、`content-type`。応答全文、prompt 全文、base64・画像・音声・動画のバイト列は入れない。
6. 例外文字列・stack・第三者 logger（`httpx`・`temporalio`・`uvicorn`・`sqlalchemy`）・`warnings`・
   未捕捉例外（`sys.excepthook`・`threading.excepthook`・asyncio）・Temporal Core（Python logging へ
   転送）も同じ処理を通す。環境変数や `Settings` の dump を出さない。
   **迂回経路**（INV-39 の対象外、`log_source=unstructured` として Collector が安全化・切詰し、短い保持の
   infra 系統へ）: logging 設定前の起動失敗・native クラッシュの stderr、migrate の alembic、
   サードパーティのコンテナ（postgres・temporal・minio）。CLI provider（Codex・Piper・git）の stderr を
   例外文に含む既存の経路は、パターンに当たらない prompt 断片を含み得る。長さ上限が唯一の防御で、
   fal の例外文も、既存の `fal_queue._short()` が応答本文の先頭（最大800字）を含め得る（構造化できない応答の fallback）。これらは §7.3 のパターン置換と長さ上限だけで守られ、**残るリスク**として扱う（既存の例外文・DB の `error_summary` は変えない）。
7. stack: 例外 chain の**各例外**について型・メッセージと、frame の先頭・末尾を残す（原因
   `__cause__`/`__context__` が先に並ぶので、全体の末尾だけを残すと根本原因が消える）。
   `ExceptionGroup` と `__notes__` も同じ。全体は `EXCEPTION_STACK_MAX_BYTES` 以下。
8. 置換・縮約をしたら `redaction_applied=true`、切り詰めたら該当の `*_truncated=true`。
9. 整形・発行の失敗は業務に伝播させない。整形器は例外を握り、最小の固定形（`event_name=log.record`、
   `message="log formatting failed"`、`error_type`）で出し直す。発行ヘルパーは record の生成を含めて
   握る。業務コードが投げる例外は同じオブジェクトを bare `raise` で再送出し、包み直さない。
10. 既存の伏せ字処理（`codex_cli.mask_secrets`、`youtube.uploader.redact`、upload の scrub、
    `url_guard.redact_url`）はそのまま残す（今回は統合しない。関係は ADR-0040 に記録）。

## 8. イベント一覧

`contracts.log_contract.EventName` の各値。発行位置（ファイル:関数）は [emission-points.md](./emission-points.md)（実装と同じコミットで更新）。

| event_name | 主な場面 |
|---|---|
| `log.record` | event_name を持たない記録（既存・第三者 logger） |
| `service.started` / `service.stopped` / `service.start_failed` | worker_entry・API の lifespan |
| `api.request.completed` | API の1リクエスト（health は DEBUG） |
| `schedule.slot.acquired` / `schedule.slot.skipped` | 日次枠の取得（commit 後）・スキップと理由 |
| `stage.started` / `stage.succeeded` / `stage.failed` / `stage.blocked` / `stage.skipped` | 各工程 workflow（replay 安全） |
| `activity.started`(DEBUG) / `activity.succeeded` / `activity.failed` | interceptor。`duration_ms`・失敗分類。cancel は `outcome=cancelled` |
| `provider.call.started`(DEBUG) / `provider.call.succeeded` / `provider.call.failed` | 外部呼び出し1回。`provider_operation` で区別。submit の受理は **ref の commit 前に** INFO で出す（§9） |
| `provider.job.state_changed` | poll で状態が変わった時だけ |
| `provider.auth_incident.recorded` / `provider.call.suppressed` | ADR-0030 の記録・抑止 |
| `reservation.reserved` / `reservation.dispatched` / `reservation.job_ref_recorded` / `reservation.spent` / `reservation.resumed` / `reservation.blocked` | 予約台帳。**commit 後だけ**（`blocked` は未照合・拒否入力で新ラウンドを止めた判断） |
| `artifact.stored` / `artifact.reused` / `artifact.reuse_rejected` / `artifact.superseded` | 成果物（stored・superseded は commit 後） |
| `scene.rejected` / `scene.input_refetch` / `scene.alternative.started` / `scene.alternative.result` | 403/422/file_download_error と既存 fallback |
| `episode.resume.requested` / `episode.resume.rejected` / `episode.resume.started` | 統一再開 |
| `render.validation.passed` / `render.validation.failed` | Render の尺・音声検証 |
| `upload.started` / `upload.succeeded` / `upload.reused_existing` / `upload.skipped` / `upload.failed` | Upload |
| `research.request.started` / `research.request.finished` | Research |
| `anomaly.recorded` | 運用異常の記録 |
| `decision.recorded` / `agent.run.recorded` | 判断記録・役割実行の記録（ADR-0041）。以下この節の行は全て `episode_events` の投影（§10） |
| `intervention.command.received` / `intervention.command.finished` | 介入 command の受付・終端（`outcome` に command の状態） |
| `intervention.comment.added` | コメント・レビュー指摘・制約の記録 |
| `intervention.hold.requested` / `intervention.hold.effective` | 停止の受付（HOLD_REQUESTED）と有効化（HELD） |
| `intervention.change.proposed` / `intervention.change.applied` / `intervention.change.conflicted` / `intervention.variant.selected` | ChangeSet の提案・適用・競合・別案の採用 |
| `review.invalidated` / `review.resolved` | manifest の変更による承認の失効・注記の解決 |
| `checkpoint.waiting` / `checkpoint.released` | 待機地点での待機・解放 |
| `intervention.resume.requested` / `intervention.resume.completed` | 再開の受付・pipeline の start 結果 |
| `intervention.mode.changed` | 運転モードの変更 |

## 9. 課金と commit 境界

- `reservation.*`・`artifact.stored`・`artifact.superseded`・`schedule.slot.acquired` は、その変更を含む
  `commit` が**戻った後**にだけ発行する。commit が失敗したら発行しない（試行の失敗は
  `activity.failed` 等に出る）。発行のために既存の書き込み順序・commit の位置・例外を変えない。
- 例外: submit の受理（`provider.call.succeeded`、`provider_operation=submit`、`provider_request_id`、
  `reservation_id`）は provider の応答を観測した**直後、ref の commit の前**に出す。ref の commit が
  失敗したとき、provider のジョブを辿れる唯一の手がかりになるため（`paid_job.py` の既存の意図）。
  これは課金の確定ではなく観測の記録で、照合は provider 側で確認して確定する。
- ログ発行が失敗しても（ロガーの故障を注入しても）台帳の状態・例外の型・制御は変わらない（INV-38 の
  機械検査）。

## 10. 判断・介入イベント: 正本とログの投影（ADR-0041 §7、設計 §14/§15）

| | 正本（canonical） | ログの投影（projection） |
|---|---|---|
| 置き場所 | PostgreSQL `episode_events`（`(episode_id, seq)` で一意、追記のみ）と、それが指す新表（`decision_records`・`intervention_commands`・`artifact_manifests` 等） | stdout の JSON 1行 → Collector → OpenSearch |
| 書く時 | 業務の変更と**同じ transaction**（`InterventionStore.append_event`） | その commit が**成功した後**（`ledger.defer`）。rollback では出ない |
| 失われたら | 失われてはならない（backup/restore の対象。`scripts/consistency-check.py`） | `scripts/intervention-reproject.py` で正本から作り直せる |
| 内容 | refs の全文（JSON） | `event_name`・`event_seq`・`episode_id`・`command_id`・`actor_type`・参照らしい refs（`*_id` 等のスカラー）だけ。本文・prompt・入れ子は落とし、キー名を `attributes.refs_omitted` に残す（INV-39） |
| 読む人 | read model・API・ReviewPacket・再投影 | 人の検索・監視だけ。**業務の判断に使わない**（INV-38） |

- `projected_at` は**再投影の処理**が stdout へ渡した時刻。live の投影は stdout へ書けたかを知れない
  （ログの故障は握る）ので付けない。`projected_at IS NULL` は「再投影で確認していない」の意味で、
  「ログに無い」とは限らない。OpenSearch に索引されたことの証明でもない。
- 再投影は同じ `event_id` で出すので、同じ index の中では `_id`（`create`）が重複を捨てる。日付の index を
  またぐと別文書になり得る（§4）。検索側は `event_id` で重複を除く。
- 遅延・未投影の監視: 正本の `episode_events` の件数（期間・Episode ごと）と OpenSearch の件数を**人が**突き合わせる。
  食い違えば再投影する。アプリは OpenSearch を読まない。
