"""構造化ログの契約（ADR-0040 / docs/observability/log-contract.md）。

**唯一の宣言元**（AGENTS.md §8）。Python の発行側（``infrastructure/logging``）と、収集・検索側
（``deploy/logging`` の index template・mapping・Fluent Bit の型検査）の両方がこの定義に合わせる。
mapping との一致は contract test が検査する。

ここにあるのは語彙と上限だけで、振る舞い（整形・安全化・文脈の引き回し）は持たない。
ログは検索用の副本であり、業務状態・課金・冪等性の自動判断に使わない（INV-38）。

モジュール名を ``logging`` にしないのは、実行位置によって標準ライブラリを隠し得るため。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: ログ契約の版。フィールドの意味・型を変えたら上げる（追加だけなら上げない）。
LOG_SCHEMA_VERSION = 1


class FieldType(StrEnum):
    """OpenSearch の mapping 型（明示 mapping。dynamic mapping に委ねない）。"""

    DATE = "date"
    KEYWORD = "keyword"
    TEXT = "text"
    INTEGER = "integer"
    LONG = "long"
    DOUBLE = "double"
    BOOLEAN = "boolean"
    #: 検索対象にしない object（``enabled: false``）。自由なキーを index に展開しない。
    OPAQUE_OBJECT = "opaque_object"


class FieldOrigin(StrEnum):
    """その値を誰が書くか。"""

    #: アプリ（Python）が stdout に出す前に書く
    APP = "app"
    #: Collector（Fluent Bit）または OpenSearch の ingest pipeline が書く
    COLLECTOR = "collector"


@dataclass(frozen=True, slots=True)
class LogField:
    name: str
    type: FieldType
    origin: FieldOrigin = FieldOrigin.APP
    #: 全イベントで必須か（該当する処理だけで必須のものは False）
    always: bool = False


_A = FieldOrigin.APP
_C = FieldOrigin.COLLECTOR
_T = FieldType

#: トップレベルのフィールド（順序は文書の表と同じ）。ここに無いキーは ``attributes`` に入れる。
LOG_FIELDS: tuple[LogField, ...] = (
    # --- 常に必須（APP）
    LogField("@timestamp", _T.DATE, always=True),
    LogField("schema_version", _T.INTEGER, always=True),
    LogField("event_id", _T.KEYWORD, always=True),
    LogField("event_name", _T.KEYWORD, always=True),
    LogField("level", _T.KEYWORD, always=True),
    LogField("message", _T.TEXT, always=True),
    LogField("service_name", _T.KEYWORD, always=True),
    LogField("environment", _T.KEYWORD, always=True),
    LogField("git_sha", _T.KEYWORD, always=True),
    LogField("logger", _T.KEYWORD, always=True),
    # --- 業務の文脈（該当する処理でだけ付く。未取得なら付けない）
    LogField("episode_id", _T.KEYWORD),
    LogField("scene_id", _T.KEYWORD),
    LogField("scene_revision", _T.INTEGER),
    LogField("storyboard_artifact_id", _T.KEYWORD),
    LogField("stage", _T.KEYWORD),
    LogField("job_id", _T.KEYWORD),
    LogField("research_request_id", _T.KEYWORD),
    LogField("research_call_id", _T.KEYWORD),
    # --- Temporal
    LogField("workflow_id", _T.KEYWORD),
    LogField("run_id", _T.KEYWORD),
    LogField("workflow_type", _T.KEYWORD),
    LogField("activity_id", _T.KEYWORD),
    LogField("activity_type", _T.KEYWORD),
    LogField("task_queue", _T.KEYWORD),
    LogField("activity_attempt", _T.INTEGER),
    # --- API・将来の Traces
    LogField("request_id", _T.KEYWORD),
    LogField("correlation_id", _T.KEYWORD),
    LogField("trace_id", _T.KEYWORD),
    LogField("span_id", _T.KEYWORD),
    LogField("http_method", _T.KEYWORD),
    LogField("http_route", _T.KEYWORD),
    # --- 外部 provider と予約台帳
    LogField("provider", _T.KEYWORD),
    LogField("provider_operation", _T.KEYWORD),
    LogField("provider_endpoint", _T.KEYWORD),
    LogField("provider_request_id", _T.KEYWORD),
    LogField("provider_attempt", _T.INTEGER),
    LogField("reservation_id", _T.KEYWORD),
    LogField("reservation_status", _T.KEYWORD),
    LogField("artifact_id", _T.KEYWORD),
    LogField("artifact_type", _T.KEYWORD),
    LogField("input_hash", _T.KEYWORD),
    # --- 結果
    LogField("outcome", _T.KEYWORD),
    LogField("http_status", _T.INTEGER),
    LogField("duration_ms", _T.DOUBLE),
    # --- 失敗（観測事実と推定分類を分ける）
    LogField("error_type", _T.KEYWORD),
    LogField("error_code", _T.KEYWORD),
    LogField("error_category", _T.KEYWORD),
    LogField("classification_basis", _T.KEYWORD),
    LogField("failure_class", _T.KEYWORD),
    LogField("retryable", _T.BOOLEAN),
    LogField("error_message", _T.TEXT),
    LogField("response_excerpt", _T.TEXT),
    LogField("exception_stack", _T.TEXT),
    # --- 安全化・切り詰めの事実
    LogField("redaction_applied", _T.BOOLEAN),
    LogField("response_truncated", _T.BOOLEAN),
    LogField("stack_truncated", _T.BOOLEAN),
    LogField("truncated", _T.BOOLEAN),
    # --- 補助情報（検索対象にしない）
    LogField("attributes", _T.OPAQUE_OBJECT),
    # --- Collector / ingest pipeline が書く
    LogField("ingested_at", _T.DATE, origin=_C),
    LogField("log_source", _T.KEYWORD, origin=_C),
    LogField("stream", _T.KEYWORD, origin=_C),
    LogField("container_name", _T.KEYWORD, origin=_C),
    LogField("compose_service", _T.KEYWORD, origin=_C),
    LogField("compose_project", _T.KEYWORD, origin=_C),
    LogField("collector_errors", _T.KEYWORD, origin=_C),
    LogField("host_name", _T.KEYWORD, origin=_C),
)

LOG_FIELD_NAMES: frozenset[str] = frozenset(f.name for f in LOG_FIELDS)
REQUIRED_APP_FIELDS: tuple[str, ...] = tuple(f.name for f in LOG_FIELDS if f.always)

#: infra（``log_source=unstructured``）系統の文書が持つフィールド。Collector が書くものの全部と、
#: Collector が JSON でない行から作る ``@timestamp``（Docker の時刻）・``message``
#: （安全化・切り詰め済みの行）・``truncated``・``redaction_applied``。
#: infra の mapping はこの集合から生成する（ADR-0040 §4）。
INFRA_FIELD_NAMES: frozenset[str] = frozenset(
    {f.name for f in LOG_FIELDS if f.origin is FieldOrigin.COLLECTOR}
    | {"@timestamp", "message", "truncated", "redaction_applied"}
)


# --------------------------------------------------------------------------- 設定キー（env）

#: compose のサービス名と同じ値（``service_name``）
ENV_SERVICE_NAME = "AVP_SERVICE_NAME"
#: ``environment``（``Environment`` の値）
ENV_ENVIRONMENT = "AVP_ENVIRONMENT"
#: ``json``（既定）| ``text``（rollback 用。従来の basicConfig 形式）
ENV_LOG_FORMAT = "AVP_LOG_FORMAT"
#: 既定 ``INFO``
ENV_LOG_LEVEL = "AVP_LOG_LEVEL"
#: イメージに焼いた git revision（既存。Dockerfile の ENV と同じ名前）
ENV_GIT_REVISION = "AVP_GIT_REVISION"

#: Collector がアプリの JSON 系統として扱うコンテナの label（json-file の ``labels`` で各行に載る）
APP_LOG_LABEL = "avp.logging"
APP_LOG_LABEL_VALUE = "app"
#: event_name を持たない記録の既定値、未設定値の表記
UNKNOWN = "unknown"
#: 発行側がイベントのフィールドを載せる LogRecord の属性名（``extra={"avp": {...}}``）。
#: LogRecord の予約属性（``message`` 等）と衝突させないため1キーにまとめる。
RECORD_EXTRA_KEY = "avp"


class Environment(StrEnum):
    PROD = "prod"
    DEV = "dev"
    TEST = "test"
    UNKNOWN = "unknown"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogStage(StrEnum):
    """``stage`` の語彙（工程）。Episode の状態値ではない。"""

    SCHEDULE = "schedule"
    PIPELINE = "pipeline"
    PLANNING = "planning"
    RESEARCH = "research"
    SCRIPT = "script"
    STORYBOARD = "storyboard"
    PRODUCTION = "production"
    IMAGE = "image"
    VOICE = "voice"
    VIDEO = "video"
    SCENE_RECOVERY = "scene_recovery"
    RENDER = "render"
    UPLOAD = "upload"
    RESUME = "resume"
    WATCHDOG = "watchdog"


class ProviderOperation(StrEnum):
    """``provider_operation``。submit・poll・download を混ぜない。"""

    SUBMIT = "submit"
    #: queue の状態確認（poll の1回）
    STATUS = "status"
    #: 完了ジョブの結果 JSON の取得
    RESULT = "result"
    #: 生成物のダウンロード
    DOWNLOAD = "download"
    #: provider の保管領域への入力のアップロード（fal storage）
    INPUT_UPLOAD = "input_upload"
    #: 入力アップロード用の一時 token 取得
    STORAGE_TOKEN = "storage_token"
    #: CLI 型 provider（Codex / Piper / OpenMontage）の1回の起動
    INVOKE = "invoke"
    #: YouTube の resumable session 開始
    UPLOAD_SESSION = "upload_session"
    #: YouTube への本体送信
    UPLOAD_MEDIA = "upload_media"
    #: YouTube の処理状態確認
    PROCESSING_CHECK = "processing_check"
    #: YouTube の動画状態の問い合わせ・自チャンネル確認・marker による既存動画検索
    QUERY_STATUS = "query_status"
    CHANNEL_LOOKUP = "channel_lookup"
    FIND_EXISTING = "find_existing"
    #: OAuth access token の更新
    OAUTH_REFRESH = "oauth_refresh"
    #: 検索・本文取得（Research）
    SEARCH = "search"
    FETCH = "fetch"


class ProviderLabel(StrEnum):
    """台帳の外の呼び出しの ``provider``。台帳の呼び出しは ``ProviderCall`` の値を使う。"""

    FAL_STORAGE = "fal_storage"
    YOUTUBE_ANALYTICS = "youtube_analytics"
    YOUTUBE_OAUTH = "youtube_oauth"
    PIPER = "piper"
    OPENMONTAGE = "openmontage"
    #: Research の検索・本文取得（provider 固有名は ``provider_endpoint``）
    RESEARCH_SEARCH = "research_search"
    RESEARCH_FETCH = "research_fetch"


class Outcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    REUSED = "reused"
    REJECTED = "rejected"
    #: 送信したが受理されたか分からない（再送しない）
    AMBIGUOUS = "ambiguous"
    STATE_CHANGED = "state_changed"
    RESUMED = "resumed"
    #: Temporal の cancel（兄弟 Activity の打ち切り等）
    CANCELLED = "cancelled"


class ErrorCategory(StrEnum):
    """**推定**分類。観測事実（``http_status`` / ``error_code``）とは別に持つ。

    ``CONTENT_POLICY`` / ``INPUT_VALIDATION`` / ``INPUT_UNREACHABLE`` / ``UNKNOWN`` の値は
    ``contracts.states.RejectionCategory`` と同じ文字列（一致は contract test が検査する）。

    既存の制御（再試行・422 fallback・needs_input）を決めるものではない（ログ専用）。
    根拠は ``classification_basis`` に残す。HTTP 403 だけでは ``ACCESS_DENIED`` であって
    credentials とは断定しない。
    """

    ACCESS_DENIED = "access_denied"
    AUTH_REJECTED = "auth_rejected"
    RATE_LIMITED = "rate_limited"
    CONTENT_POLICY = "content_policy"
    INPUT_VALIDATION = "input_validation"
    INPUT_UNREACHABLE = "input_unreachable"
    PROVIDER_JOB_FAILED = "provider_job_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SUBMIT_AMBIGUOUS = "submit_ambiguous"
    TIMEOUT = "timeout"
    TRANSIENT_NETWORK = "transient_network"
    MEDIA_VALIDATION = "media_validation"
    UNRECONCILED_RESERVATION = "unreconciled_reservation"
    SUPPRESSED_BY_INCIDENT = "suppressed_by_incident"
    CONFIGURATION = "configuration"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


class ClassificationBasis(StrEnum):
    """``error_category`` を何から推定したか。"""

    HTTP_STATUS_ONLY = "http_status_only"
    PROVIDER_ERROR_TYPE = "provider_error_type"
    PROVIDER_HEADER = "provider_header"
    EXCEPTION_TYPE = "exception_type"
    NONE = "none"


class LogSource(StrEnum):
    """Collector が付ける、その行の由来。"""

    #: 契約どおりの JSON（アプリ）
    APP_JSON = "app_json"
    #: JSON でない行（第三者プロセス・インフラ）。安全化・切り詰めのうえ別系統へ
    UNSTRUCTURED = "unstructured"


class EventName(StrEnum):
    """固定イベント名。増やすときはここへ足し、log-contract.md の一覧も同じコミットで更新する。"""

    # 汎用（event_name を持たない第三者・既存 logger の記録）
    LOG_RECORD = "log.record"
    # サービス
    SERVICE_STARTED = "service.started"
    SERVICE_STOPPED = "service.stopped"
    SERVICE_START_FAILED = "service.start_failed"
    # API
    API_REQUEST_COMPLETED = "api.request.completed"
    # Schedule / 日次枠
    SCHEDULE_SLOT_ACQUIRED = "schedule.slot.acquired"
    SCHEDULE_SLOT_SKIPPED = "schedule.slot.skipped"
    # 工程（Workflow から replay 安全に発行）
    STAGE_STARTED = "stage.started"
    STAGE_SUCCEEDED = "stage.succeeded"
    STAGE_FAILED = "stage.failed"
    STAGE_BLOCKED = "stage.blocked"
    STAGE_SKIPPED = "stage.skipped"
    # Activity（worker interceptor）
    ACTIVITY_STARTED = "activity.started"
    ACTIVITY_SUCCEEDED = "activity.succeeded"
    ACTIVITY_FAILED = "activity.failed"
    # 外部 provider 呼び出し（provider_operation で submit/status/result/download 等を区別）
    PROVIDER_CALL_STARTED = "provider.call.started"
    PROVIDER_CALL_SUCCEEDED = "provider.call.succeeded"
    PROVIDER_CALL_FAILED = "provider.call.failed"
    PROVIDER_JOB_STATE_CHANGED = "provider.job.state_changed"
    PROVIDER_AUTH_INCIDENT_RECORDED = "provider.auth_incident.recorded"
    PROVIDER_CALL_SUPPRESSED = "provider.call.suppressed"
    # 予約台帳（DB commit が戻った後にだけ発行）
    RESERVATION_RESERVED = "reservation.reserved"
    RESERVATION_DISPATCHED = "reservation.dispatched"
    RESERVATION_JOB_REF_RECORDED = "reservation.job_ref_recorded"
    RESERVATION_SPENT = "reservation.spent"
    RESERVATION_RESUMED = "reservation.resumed"
    RESERVATION_BLOCKED = "reservation.blocked"
    # 成果物
    ARTIFACT_STORED = "artifact.stored"
    ARTIFACT_REUSED = "artifact.reused"
    ARTIFACT_REUSE_REJECTED = "artifact.reuse_rejected"
    ARTIFACT_SUPERSEDED = "artifact.superseded"
    # シーン単位の拒否・復旧（403/422/file_download_error と既存 fallback）
    SCENE_REJECTED = "scene.rejected"
    SCENE_INPUT_REFETCH = "scene.input_refetch"
    SCENE_ALTERNATIVE_STARTED = "scene.alternative.started"
    SCENE_ALTERNATIVE_RESULT = "scene.alternative.result"
    # 再開
    EPISODE_RESUME_REQUESTED = "episode.resume.requested"
    EPISODE_RESUME_REJECTED = "episode.resume.rejected"
    EPISODE_RESUME_STARTED = "episode.resume.started"
    # Render / Upload
    RENDER_VALIDATION_PASSED = "render.validation.passed"
    RENDER_VALIDATION_FAILED = "render.validation.failed"
    UPLOAD_STARTED = "upload.started"
    UPLOAD_SUCCEEDED = "upload.succeeded"
    UPLOAD_REUSED_EXISTING = "upload.reused_existing"
    UPLOAD_SKIPPED = "upload.skipped"
    UPLOAD_FAILED = "upload.failed"
    # Research
    RESEARCH_REQUEST_STARTED = "research.request.started"
    RESEARCH_REQUEST_FINISHED = "research.request.finished"
    # 運用異常（既存 operational_anomalies の記録）
    ANOMALY_RECORDED = "anomaly.recorded"


# --------------------------------------------------------------------------- 上限（UTF-8 bytes）

#: provider の応答から抜き出した診断情報
RESPONSE_EXCERPT_MAX_BYTES = 4096
#: 例外の stack
EXCEPTION_STACK_MAX_BYTES = 8192
#: message / error_message
MESSAGE_MAX_BYTES = 2048
ERROR_MESSAGE_MAX_BYTES = 1024
#: attributes を JSON にした大きさ
ATTRIBUTES_MAX_BYTES = 4096
#: 1イベント（1行・改行を除く。``ensure_ascii=False`` で直列化した後の UTF-8 bytes）。
#: Docker json-file はアプリが書いた生の行を 16384 bytes で partial に分割するので、それを下回らせる
#: （余裕は Collector 側の tail buffer とエスケープ増分のため）
EVENT_MAX_BYTES = 12288
#: keyword 1値の上限（mapping の ignore_above と同じ値）。発行側はこれを超える値を切り詰め
#: ``truncated=true`` にする（検索できない値を黙って残さない）
KEYWORD_MAX_CHARS = 512
#: JSON でない行（``log_source=unstructured``）を Collector が切り詰める上限
UNSTRUCTURED_LINE_MAX_BYTES = 4096
#: Collector が受け付ける1行（Docker の partial を結合した後の ``log``）の上限。超えた行は
#: JSON として解釈せず、この長さで切ってから infra 系統へ送る
#: （``collector_errors=line_too_long``）。
#: tail の ``buffer_max_size`` と同じ値（fluent-bit.yaml）: partial は 16KiB ごとに判定されるので、
#: 結合後の行には ``buffer_max_size`` が効かない（実測、I-15）
COLLECTOR_LINE_MAX_BYTES = 262144

#: 秘密を置き換えた印
REDACTED = "[REDACTED]"


__all__ = [
    "APP_LOG_LABEL",
    "APP_LOG_LABEL_VALUE",
    "ATTRIBUTES_MAX_BYTES",
    "COLLECTOR_LINE_MAX_BYTES",
    "ENV_ENVIRONMENT",
    "ENV_GIT_REVISION",
    "ENV_LOG_FORMAT",
    "ENV_LOG_LEVEL",
    "ENV_SERVICE_NAME",
    "ERROR_MESSAGE_MAX_BYTES",
    "EVENT_MAX_BYTES",
    "EXCEPTION_STACK_MAX_BYTES",
    "KEYWORD_MAX_CHARS",
    "LOG_FIELDS",
    "LOG_FIELD_NAMES",
    "LOG_SCHEMA_VERSION",
    "MESSAGE_MAX_BYTES",
    "RECORD_EXTRA_KEY",
    "REDACTED",
    "INFRA_FIELD_NAMES",
    "REQUIRED_APP_FIELDS",
    "RESPONSE_EXCERPT_MAX_BYTES",
    "UNKNOWN",
    "UNSTRUCTURED_LINE_MAX_BYTES",
    "ClassificationBasis",
    "Environment",
    "ErrorCategory",
    "EventName",
    "FieldOrigin",
    "FieldType",
    "LogField",
    "LogLevel",
    "LogSource",
    "LogStage",
    "Outcome",
    "ProviderLabel",
    "ProviderOperation",
]
