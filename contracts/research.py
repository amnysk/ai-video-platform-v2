"""Research（Trend / Evidence）の共通契約（ADR-0037）。

**research の語彙の唯一の宣言元**（AGENTS.md §8）。

Research は本番の Episode 工程とは別の永続化を持つ（ADR-0037。本番の ``JobType`` /
``ArtifactType`` / ``ProviderCall``（``contracts/states.py``）には値を足さない）。ここに置くもの:

- 語彙: 依頼の種別・状態、外部呼び出しの種別（台帳の枠）と状態、research の成果物の型、
  止めた理由のコード（``ResearchStopCode``）、Provider の設定値（``ResearchProviderMode``）
- 版・既定値（鮮度・検索/取得の上限と天井）
- 依頼 ``ResearchRequestSpec``: **種別（kind）で判別する union**。Trend にしか無い項目と
  Evidence にしか無い項目を型で分け、``extra="forbid"`` で越境を拒否する
- 結果 ``ResearchResult``（``research_requests.result_summary`` の形）

依頼は **絶対日時**（``time_window``）と ``as_of``（tz 必須）を持つ。壁時計を契約の中で読まない。
DB の型はここに置かない（INV-6）。

Temporal の境界（ADR-0037 §8.5）: task queue・workflow 名・workflow id の規約・Activity 名と、
Activity の引数・戻り値の dataclass（ADR-0029 の「型注釈どおりの形」。成果物の本体は載せず、
参照と件数だけ）もここが唯一の宣言元。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, get_args

from pydantic import (
    AwareDatetime,
    Field,
    StrictInt,
    TypeAdapter,
    field_validator,
    model_validator,
)

from contracts.artifact_refs import (
    SHA256_HEX_PATTERN,
    ArtifactDigestRef,
    FrozenModel,
    check_canonical_uuid,
)
from contracts.upload import YOUTUBE_VIDEO_ID_PATTERN

# ------------------------------------------------------------------ 語彙（migration 0015 が凍結）


class ResearchKind(StrEnum):
    """調査の種別。"""

    TREND = "trend"
    EVIDENCE = "evidence"


class ResearchStatus(StrEnum):
    """調査依頼の実行状態（``domain/research/status.py`` の表）。

    ``completed`` / ``partial`` / ``failed`` が終端。``partial`` は**合格ではない**。
    実行状態は主張の評価（supported 等）とは別物。
    """

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


class ResearchCall(StrEnum):
    """外部呼び出しの種別。**呼び出し台帳（``research_calls``）の枠の単位**（INV-36）。

    枠は検索エンジン（Web / YouTube）ごとではなく種別ごとに1つ。どの Adapter が呼ばれたかは
    台帳の ``provider`` 列（診断用のラベル）に残す。
    """

    SEARCH = "search"
    FETCH = "fetch"
    ASSESS = "assess"


class ResearchCallStatus(StrEnum):
    """台帳の1行の状態。``reserved`` / ``spent`` / ``abandoned`` の**すべてが枠を数える**。"""

    RESERVED = "reserved"
    SPENT = "spent"
    #: 送っていないことが確かな予約（``dispatched_at`` が NULL のまま）を手放した。
    ABANDONED = "abandoned"


class ResearchArtifactType(StrEnum):
    """research が所有する成果物の型（``research_artifacts``）。

    Episode の ``ArtifactType`` とは別。
    """

    RESEARCH_TREND = "research_trend"
    RESEARCH_EVIDENCE = "research_evidence"
    # 台本の主張を Evidence と照合した結果（ADR-0038 で使う。Episode の Artifact ではない）
    RESEARCH_SCRIPT_VERIFICATION = "research_script_verification"


class ResearchStopCode(StrEnum):
    """調査を止めた・縮めた理由のコード（ADR-0037 §6 / §8）。**唯一の定義**。

    ``blocked`` の ``blocked_reason.code`` と、``ResearchResult.stop_code``
    （``partial`` / ``failed`` の理由）に入る。人と API が読む値で、DB の CHECK には載せない。
    """

    #: Provider が ``none`` / 未設定。外部を呼ばずに止める
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    #: 実 Provider なのに ``max_cost_usd`` / ``max_youtube_units`` のどちらかが未設定
    BUDGET_NOT_SET = "budget_not_set"
    #: その種別（Trend / Evidence）の Handler が登録されていない
    HANDLER_NOT_AVAILABLE = "handler_not_available"
    #: 評価器（``EvidenceAssessor`` / ``TrendInterpreter``）が組まれていない。評価できなかった
    #: claim は ``insufficient`` のまま・Trend は観測だけで解釈を持たないまま、依頼は
    #: ``partial``（合格にしない。ADR-0038 / ADR-0039）
    ASSESSOR_NOT_AVAILABLE = "assessor_not_available"
    #: 呼び出し台帳の件数・金額・quota の上限に達した（INV-36）
    CALL_BUDGET_EXHAUSTED = "call_budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    #: 認証・認可の拒否（人手でしか直らない）
    PROVIDER_AUTH = "provider_auth"
    #: dispatch 済みで結果の無い呼び出しがある。再送しない・解放しない（人手照合）
    AMBIGUOUS_CALL = "ambiguous_call"
    #: どの検索も使える結果を返さなかった（恒久的な失敗）
    NO_USABLE_RESULTS = "no_usable_results"
    #: Activity の retry を使い切った・想定外の失敗（Worker が記録する）
    EXECUTION_FAILED = "execution_failed"


# ------------------------------------------------------------------ Provider の設定値

#: ``RESEARCH_PROVIDER`` の値。registry が組めるのはこの 2 つだけ
#: （実 Provider は所有者の判断と ADR を待つ）。
ResearchProviderMode = Literal["fake", "none"]
RESEARCH_PROVIDER_MODES: tuple[str, ...] = get_args(ResearchProviderMode)
#: 既定は ``none``（依頼は ``blocked``。fail-closed）
DEFAULT_RESEARCH_PROVIDER: ResearchProviderMode = "none"


# ------------------------------------------------------------------ 版（request_hash に入る）

RESEARCH_POLICY_VERSION = "research-policy-1"
RESEARCH_SCHEMA_VERSION = "1.0"
RESEARCH_PROMPT_VERSION = "research-prompt-1"
PROVIDER_CONFIG_VERSION = "provider-config-1"

# ------------------------------------------------------------------ 鮮度（再利用の窓。ADR-0037 §5）

#: この時間以内に完了した同じ意味の Trend は再利用する。読む側（ADR-0039 §2）はこの時間以内の観測を
#: ``fresh`` とする（どちらも設定値 ``trend_fresh_hours`` の既定値）。
TREND_FRESH_HOURS = 24
#: 観測からこの日数までの Trend は ``stale``（日時つき）として読める。それより古い Trend は ``none``
#: （「Trend 無し」。ADR-0039 §2）。
TREND_STALE_MAX_DAYS = 7
#: Evidence の再確認期限（史実の有効期限ではなく、資料の再取得を促す運用値）。
EVIDENCE_REVERIFY_DAYS = 30

# ------------------------------------------------------------------ 上限の既定と天井（ADR-0037 §4）

DEFAULT_MAX_SEARCHES = 5
DEFAULT_MAX_FETCHES = 10
DEFAULT_MAX_ASSESSMENTS = 5
#: 追加調査・台本修正ループの上限。外部通信 retry の回数とは別のカウンタ。
DEFAULT_MAX_FOLLOWUP_ROUNDS = 2
DEFAULT_DEADLINE_SECONDS = 1800

#: 上限値そのものの暴走を止める天井（``ResearchLimits`` の検証）。既定より十分大きい。
LIMIT_CEILING_SEARCHES = 50
LIMIT_CEILING_FETCHES = 100
LIMIT_CEILING_ASSESSMENTS = 50
LIMIT_CEILING_FOLLOWUP_ROUNDS = 5
LIMIT_CEILING_DEADLINE_SECONDS = 24 * 60 * 60

#: 1 依頼に載せる claim の最大数。
MAX_CLAIMS_PER_REQUEST = 12
CLAIM_TEXT_MAX_CHARS = 300

ResearchLanguage = Literal["ja", "en"]
FormatProfile = Literal["shorts", "long", "any"]


class ClaimKind(StrEnum):
    """主張の種類。「最初/唯一/最大」等の強い断定は ``superlative``。"""

    YEAR = "year"
    PERSON = "person"
    PLACE = "place"
    EVENT = "event"
    QUANTITY = "quantity"
    COMPARISON = "comparison"
    CAUSAL = "causal"
    SUPERLATIVE = "superlative"


class ClaimImportance(StrEnum):
    CENTRAL = "central"
    SUPPORTING = "supporting"


def normalize_claim_text(text: str) -> str:
    """正規化本文。NFKC で全半角を畳み、大文字小文字（casefold）と空白を畳む。

    ``request_hash`` と claim の重複判定が使う**唯一の正規化**。
    """
    folded = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(folded.split())


def refresh_slot_of(as_of: datetime) -> str:
    """Trend の日次の枠。``as_of`` の **UTC 日付**（Trend の hash を日単位に丸める）。"""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return as_of.astimezone(UTC).date().isoformat()


# ------------------------------------------------------------------ 共通部品


class TimeWindow(FrozenModel):
    """調査対象期間。**絶対日時**（「直近 7 日」のような相対表現は依頼に入れない）。"""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")
        return self


def _positive(ceiling: int) -> Any:
    return Field(strict=True, gt=0, le=ceiling)


class ResearchLimits(FrozenModel):
    """依頼に**凍結**する上限。実行時に環境変数を読み直さない。

    ``max_searches`` / ``max_fetches`` / ``max_assessments`` は呼び出し台帳の種別ごとの枠
    （``call_ceiling``。INV-36）。``max_youtube_units`` と ``max_cost_usd`` は Provider 確定前は
    ``None``。``None`` の実 Provider の依頼は呼び出しの前に ``blocked`` にする（ADR-0037 §6）。
    """

    max_searches: Annotated[StrictInt, _positive(LIMIT_CEILING_SEARCHES)] = DEFAULT_MAX_SEARCHES
    max_fetches: Annotated[StrictInt, _positive(LIMIT_CEILING_FETCHES)] = DEFAULT_MAX_FETCHES
    max_assessments: Annotated[StrictInt, _positive(LIMIT_CEILING_ASSESSMENTS)] = (
        DEFAULT_MAX_ASSESSMENTS
    )
    max_followup_rounds: Annotated[StrictInt, _positive(LIMIT_CEILING_FOLLOWUP_ROUNDS)] = (
        DEFAULT_MAX_FOLLOWUP_ROUNDS
    )
    max_youtube_units: Annotated[StrictInt, Field(strict=True, gt=0)] | None = None
    max_cost_usd: Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=4)] | None = None
    deadline_seconds: Annotated[StrictInt, _positive(LIMIT_CEILING_DEADLINE_SECONDS)] = (
        DEFAULT_DEADLINE_SECONDS
    )


def call_ceiling(limits: ResearchLimits, call: ResearchCall) -> int:
    """1 依頼で ``call`` の種別に使ってよい呼び出し件数（INV-36）。定義はここだけ。"""
    ceilings = {
        ResearchCall.SEARCH: limits.max_searches,
        ResearchCall.FETCH: limits.max_fetches,
        ResearchCall.ASSESS: limits.max_assessments,
    }
    return ceilings[call]


class ClaimInput(FrozenModel):
    """確認したい主張 1 件（Evidence の入力）。URL・書誌は入れない（LLM に作らせない）。"""

    claim_text: str = Field(min_length=1, max_length=CLAIM_TEXT_MAX_CHARS)
    kind: ClaimKind
    era: str | None = Field(default=None, min_length=1, max_length=100)
    region: str | None = Field(default=None, min_length=1, max_length=100)
    importance: ClaimImportance

    @field_validator("claim_text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not normalize_claim_text(value):
            raise ValueError("claim_text must not be blank")
        return value


REGION_CODE_PATTERN = r"^[A-Z]{2}$"
MAX_TREND_SEED_TERMS = 10
MAX_TREND_PAST_VIDEOS = 50
MAX_INPUT_ARTIFACT_REFS = 20
SEED_TERM_MAX_CHARS = 100


class TrendResearchInputs(FrozenModel):
    """Trend にしか無い入力。Evidence の入力（claim）はここに入れられない。"""

    #: 視聴可能地域の絞り込み（ISO 3166-1 alpha-2）。「その地域の視聴者への人気」の保証ではない。
    region: str = Field(pattern=REGION_CODE_PATTERN)
    #: 「この観客に試す価値がある」という**仮説**。測定値ではない。
    audience_hypothesis: str = Field(min_length=1, max_length=300)
    seed_terms: Annotated[tuple[str, ...], Field(max_length=MAX_TREND_SEED_TERMS)] = ()
    #: 過去に投稿した動画（重複を避ける）。YouTube video id。
    past_video_refs: Annotated[tuple[str, ...], Field(max_length=MAX_TREND_PAST_VIDEOS)] = ()
    #: 自チャンネル Analytics の snapshot（``analytics_snapshots.id``）。区分のまま参照するだけ。
    analytics_ref: str | None = None

    @field_validator("seed_terms")
    @classmethod
    def _seed_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [normalize_claim_text(term) for term in value]
        if any(not term for term in normalized) or any(len(t) > SEED_TERM_MAX_CHARS for t in value):
            raise ValueError("seed_terms must be non-blank and at most 100 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("seed_terms must be unique after normalization")
        return value

    @field_validator("past_video_refs")
    @classmethod
    def _past_video_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for video_id in value:
            if re.fullmatch(YOUTUBE_VIDEO_ID_PATTERN, video_id) is None:
                raise ValueError(f"past_video_refs must be YouTube video ids: {video_id!r}")
        if len(set(value)) != len(value):
            raise ValueError("past_video_refs must be unique")
        return value

    @field_validator("analytics_ref")
    @classmethod
    def _analytics_ref(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)


class EvidenceResearchInputs(FrozenModel):
    """Evidence にしか無い入力: 確認したい主張の列。Trend の入力はここに入れられない。"""

    claim_inputs: Annotated[
        tuple[ClaimInput, ...], Field(min_length=1, max_length=MAX_CLAIMS_PER_REQUEST)
    ]

    @model_validator(mode="after")
    def _unique_claims(self) -> EvidenceResearchInputs:
        normalized = [normalize_claim_text(c.claim_text) for c in self.claim_inputs]
        if len(set(normalized)) != len(normalized):
            raise ValueError("claim_text must be unique after normalization")
        return self


# ------------------------------------------------------------------ 依頼


class ResearchRequestBase(FrozenModel):
    """依頼の共通部。種別固有の入力は継承先の ``inputs`` にだけ置く。"""

    requester: str = Field(min_length=1, max_length=64)
    channel_id: str = Field(min_length=1, max_length=64)
    #: 参照であって所有ではない（Evidence は複数 Episode が共有しうる）。FK も張らない。
    episode_id: str | None = None
    audience: str = Field(min_length=1, max_length=200)
    language: ResearchLanguage
    format_profile: FormatProfile
    time_window: TimeWindow
    #: 基準日時。依頼時に確定する（Workflow で壁時計を読まない）。**tz 必須**。
    as_of: AwareDatetime
    input_artifact_refs: Annotated[
        tuple[ArtifactDigestRef, ...], Field(max_length=MAX_INPUT_ARTIFACT_REFS)
    ] = ()
    limits: ResearchLimits = Field(default_factory=ResearchLimits)
    policy_version: str = Field(default=RESEARCH_POLICY_VERSION, min_length=1, max_length=64)
    schema_version: str = RESEARCH_SCHEMA_VERSION

    @field_validator("episode_id")
    @classmethod
    def _episode_id(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)

    @field_validator("schema_version")
    @classmethod
    def _schema_version(cls, value: str) -> str:
        if value != RESEARCH_SCHEMA_VERSION:
            raise ValueError(f"unsupported research schema_version: {value!r}")
        return value

    @model_validator(mode="after")
    def _unique_refs(self) -> ResearchRequestBase:
        keys = [(ref.artifact_id, ref.sha256) for ref in self.input_artifact_refs]
        if len(set(keys)) != len(keys):
            raise ValueError("input_artifact_refs must be unique")
        return self


class TrendResearchRequest(ResearchRequestBase):
    kind: Literal[ResearchKind.TREND]
    inputs: TrendResearchInputs


class EvidenceResearchRequest(ResearchRequestBase):
    kind: Literal[ResearchKind.EVIDENCE]
    inputs: EvidenceResearchInputs


#: 種別（``kind``）で判別する依頼。検証済みの正準 JSON が ``research_requests.payload`` に入る。
ResearchRequestSpec = Annotated[
    TrendResearchRequest | EvidenceResearchRequest, Field(discriminator="kind")
]
RESEARCH_SPEC_ADAPTER: TypeAdapter[TrendResearchRequest | EvidenceResearchRequest] = TypeAdapter(
    ResearchRequestSpec
)

#: 依頼の冪等キー。呼び出し側が与え、Temporal の attempt を含めない。
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,199}$"


class TrendResearchSubmit(TrendResearchRequest):
    """依頼の受け付け（Trend）。``idempotency_key`` 必須。"""

    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)


class EvidenceResearchSubmit(EvidenceResearchRequest):
    """依頼の受け付け（Evidence）。``idempotency_key`` 必須。"""

    idempotency_key: str = Field(pattern=IDEMPOTENCY_KEY_PATTERN)


ResearchSubmit = Annotated[
    TrendResearchSubmit | EvidenceResearchSubmit, Field(discriminator="kind")
]
RESEARCH_SUBMIT_ADAPTER: TypeAdapter[TrendResearchSubmit | EvidenceResearchSubmit] = TypeAdapter(
    ResearchSubmit
)


def parse_research_spec(payload: dict[str, Any]) -> TrendResearchRequest | EvidenceResearchRequest:
    """取り込み側。未知の kind・余計なキーは推測せず ``ValidationError``。"""
    return RESEARCH_SPEC_ADAPTER.validate_python(payload)


def parse_research_submit(
    payload: dict[str, Any],
) -> TrendResearchSubmit | EvidenceResearchSubmit:
    return RESEARCH_SUBMIT_ADAPTER.validate_python(payload)


def submit_to_spec(
    submit: TrendResearchSubmit | EvidenceResearchSubmit,
) -> TrendResearchRequest | EvidenceResearchRequest:
    """受け付けた本体から ``idempotency_key`` を除いた依頼を作る。

    ``idempotency_key`` は依頼の意味の一部ではない（``request_hash`` にも入れない）。
    """
    return RESEARCH_SPEC_ADAPTER.validate_python(submit.model_dump(exclude={"idempotency_key"}))


# ------------------------------------------------------------------ 結果


class ResearchArtifactRef(FrozenModel):
    """結果が指す research 所有の成果物（``research_artifacts`` の行）。"""

    artifact_type: ResearchArtifactType
    artifact_id: str
    sha256: str = Field(pattern=SHA256_HEX_PATTERN)

    @field_validator("artifact_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        return check_canonical_uuid(value)


class ResearchCoverage(FrozenModel):
    """調べた範囲。予算・取得失敗で一部しか評価できなかった依頼（``partial``）はここに不足を書く。"""

    items_requested: int = Field(ge=0)
    items_covered: int = Field(ge=0)
    not_covered: Annotated[tuple[str, ...], Field(max_length=100)] = ()

    @model_validator(mode="after")
    def _consistent(self) -> ResearchCoverage:
        if self.items_covered > self.items_requested:
            raise ValueError("items_covered must not exceed items_requested")
        if any(not item.strip() or len(item) > 300 for item in self.not_covered):
            raise ValueError("not_covered entries must be non-blank and at most 300 characters")
        return self


class ResearchUsage(FrozenModel):
    """実際に使った件数と見積もり費用。上限は ``ResearchLimits``。"""

    searches: int = Field(ge=0, default=0)
    fetches: int = Field(ge=0, default=0)
    assessments: int = Field(ge=0, default=0)
    youtube_units: int = Field(ge=0, default=0)
    cost_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=4, default=Decimal(0))


class ResearchResult(FrozenModel):
    """依頼の結果の要約（``research_requests.result_summary``）。本体は成果物にある。

    ``execution_status`` は**調査処理の実行結果**であって、claim の評価（supported 等）ではない。
    """

    request_id: str
    execution_status: ResearchStatus
    artifact_refs: Annotated[tuple[ResearchArtifactRef, ...], Field(max_length=10)] = ()
    coverage: ResearchCoverage
    warnings: Annotated[tuple[str, ...], Field(max_length=50)] = ()
    usage: ResearchUsage = Field(default_factory=ResearchUsage)
    #: 止めた・縮めた理由（``completed`` なら ``None``）。ADR-0037 §8
    stop_code: ResearchStopCode | None = None

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return check_canonical_uuid(value)

    @field_validator("warnings")
    @classmethod
    def _warnings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not w.strip() or len(w) > 300 for w in value):
            raise ValueError("warnings must be non-blank and at most 300 characters")
        return value

    @model_validator(mode="after")
    def _artifacts_match_status(self) -> ResearchResult:
        finished = {ResearchStatus.COMPLETED, ResearchStatus.PARTIAL}
        if self.execution_status in finished and not self.artifact_refs:
            raise ValueError("a completed/partial result must reference its artifact")
        if self.execution_status not in finished and self.artifact_refs:
            raise ValueError("only a completed/partial result may reference artifacts")
        return self


# ------------------------------------------------------------------ Temporal（ADR-0037 §8.5）

#: research-worker が poll する task queue。**唯一の定義**（worker・starter・compose の検査が引く）
RESEARCH_TASK_QUEUE = "research"
RESEARCH_WORKFLOW_NAME = "ResearchWorkflow"
#: (workflow 名, task queue)。``contracts/states.py`` の ``*_WORKFLOW`` と同じ形
RESEARCH_WORKFLOW: tuple[str, str] = (RESEARCH_WORKFLOW_NAME, RESEARCH_TASK_QUEUE)
#: Activity 名（workflow は名前で呼ぶ。実装を import しない。INV-3）
RESEARCH_EXECUTE_ACTIVITY = "research_execute"
RESEARCH_RECORD_FAILURE_ACTIVITY = "research_record_failure"
#: ``research_execute`` の試行回数の上限（1 回目を含む）。retry は executor が新しい番号の予約を
#: 取るので、retry も合計で呼び出しの枠を数える（INV-36）
RESEARCH_EXECUTE_MAX_ATTEMPTS = 3


def research_workflow_id(request_id: str) -> str:
    """依頼 1 件につき 1 つの workflow id（同じ依頼を同時に 2 つ走らせない）。"""
    return f"research-{check_canonical_uuid(request_id)}"


@dataclass
class ResearchWorkflowInput:
    """``ResearchWorkflow`` の入力。依頼の中身は DB にあり、履歴には ID だけを載せる。"""

    request_id: str


@dataclass
class ResearchExecuteRequest:
    request_id: str


@dataclass
class ResearchRecordFailureRequest:
    """retry を使い切った・想定外の失敗の記録（型名で分類する。ADR-0037 §8.4）。"""

    request_id: str
    error_type: str | None
    summary: str


@dataclass
class ResearchArtifactPointer:
    """成果物の参照（``research_artifacts`` の行）。本体は ArtifactStore にあり、履歴に載せない。"""

    artifact_type: str
    artifact_id: str
    sha256: str


@dataclass
class ResearchWorkflowOutput:
    """``research_execute`` / ``research_record_failure`` / workflow の結果。

    状態の正本は DB（``research_requests``）。ここは参照と件数だけ（ADR-0029 の形。金額は
    ``Decimal`` の文字列表現）。
    """

    request_id: str
    status: str
    stop_code: str | None = None
    artifact_refs: list[ResearchArtifactPointer] = field(default_factory=list)
    searches: int = 0
    fetches: int = 0
    assessments: int = 0
    youtube_units: int = 0
    cost_usd: str = "0"


__all__ = [
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_MAX_ASSESSMENTS",
    "DEFAULT_MAX_FETCHES",
    "DEFAULT_MAX_FOLLOWUP_ROUNDS",
    "DEFAULT_MAX_SEARCHES",
    "DEFAULT_RESEARCH_PROVIDER",
    "EVIDENCE_REVERIFY_DAYS",
    "PROVIDER_CONFIG_VERSION",
    "RESEARCH_PROVIDER_MODES",
    "RESEARCH_POLICY_VERSION",
    "RESEARCH_PROMPT_VERSION",
    "RESEARCH_EXECUTE_ACTIVITY",
    "RESEARCH_EXECUTE_MAX_ATTEMPTS",
    "RESEARCH_RECORD_FAILURE_ACTIVITY",
    "RESEARCH_SCHEMA_VERSION",
    "RESEARCH_TASK_QUEUE",
    "RESEARCH_WORKFLOW",
    "RESEARCH_WORKFLOW_NAME",
    "TREND_FRESH_HOURS",
    "TREND_STALE_MAX_DAYS",
    "ClaimImportance",
    "ClaimInput",
    "ClaimKind",
    "EvidenceResearchInputs",
    "EvidenceResearchRequest",
    "EvidenceResearchSubmit",
    "ResearchArtifactPointer",
    "ResearchArtifactRef",
    "ResearchArtifactType",
    "ResearchCall",
    "ResearchCallStatus",
    "ResearchCoverage",
    "ResearchExecuteRequest",
    "ResearchKind",
    "ResearchLimits",
    "ResearchProviderMode",
    "ResearchRecordFailureRequest",
    "ResearchRequestBase",
    "ResearchRequestSpec",
    "ResearchResult",
    "ResearchStatus",
    "ResearchStopCode",
    "ResearchSubmit",
    "ResearchUsage",
    "ResearchWorkflowInput",
    "ResearchWorkflowOutput",
    "TimeWindow",
    "TrendResearchInputs",
    "TrendResearchRequest",
    "TrendResearchSubmit",
    "call_ceiling",
    "normalize_claim_text",
    "parse_research_spec",
    "parse_research_submit",
    "refresh_slot_of",
    "research_workflow_id",
    "submit_to_spec",
]
