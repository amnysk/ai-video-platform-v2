"""Trend Research の成果物の契約（ADR-0039）。**``research_trend`` の唯一の宣言元**。

``research_trend``（型は ``contracts/research.py`` の ``ResearchArtifactType``。
migration 0015 が凍結済み）は research が所有する成果物で、``episode_id`` を持たない。
設計の柱（どれもこの契約の validator とテストで固定する）:

- **観測（``observations``）と解釈（``interpretations``）は別のフィールド**。
  観測は Provider が返した値と、そこから計算した値だけで、どれも ``observed_at`` を持つ。
  解釈は ``kind="hypothesis"`` だけで、根拠の観測 ID を必ず持つ。
  **存在しない観測 ID・別の候補の観測を指す解釈は作れない**
- **単一の総合スコア・順位を持たない**。候補の指標は個別（``CandidateMetrics``）で、
  欄名にも ``score`` / ``rank`` を入れない。値が無ければ ``unknown`` と理由を書く
  （**欠損を 0 にしない**）
- 増加速度は 2 つの名前でしか表せない: 同じ動画の **2 時点以上の観測の差分**
  ``views_per_hour_delta`` と、累積 ÷ 公開からの時間の**参考値**
  ``lifetime_average_views_per_hour``。「直近の伸び」に見える名前は語彙に無い
- 形式（Shorts / 長尺）は**依頼の値**（``format_basis="requested"``）で、動画の長さ
  （``videoDuration`` / ``duration_seconds``）から推定しない。依頼の値なので確からしさは
  ``low`` に固定する。候補にも形式の欄は無い
- ``region`` は視聴可能地域の絞り込み（``region_meaning``）で、視聴者の地域の保証ではない。
  ``audience_hypothesis`` は**仮説**（``kind="hypothesis"``・``measured=False``）で、
  測定値ではない

解釈器（LLM / Fake）の出力 schema（``InterpretationProposal``）もここに置く。実 LLM の
strict な出力 schema になるので、全 object が ``extra=forbid`` で全 property が required
（既定値を持たない）。提案は確定ではなく、``domain/research/trend_handler.py`` が検査して
採用する（修復しない）。
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.artifact_refs import FrozenModel, check_canonical_uuid
from contracts.research import (
    REGION_CODE_PATTERN,
    RESEARCH_POLICY_VERSION,
    FormatProfile,
    ResearchArtifactType,
    ResearchLanguage,
    TimeWindow,
)
from contracts.research_evidence import validate_http_url
from contracts.upload import YOUTUBE_CHANNEL_ID_PATTERN

TREND_ARTIFACT_SCHEMA_VERSION = "1.0"

CANDIDATE_ID_PATTERN = r"^T-[0-9]{3}$"
OBSERVATION_ID_PATTERN = r"^O-[0-9]{3}$"
INTERPRETATION_ID_PATTERN = r"^I-[0-9]{3}$"
ANGLE_ID_PATTERN = r"^A-[0-9]{3}$"

TREND_MAX_QUERIES = 20
TREND_MAX_CANDIDATES = 30
TREND_MAX_REFERENCES = 20
TREND_MAX_OBSERVATIONS = 600
TREND_MAX_INTERPRETATIONS = 100
TREND_MAX_ANGLES = 30
TREND_MAX_LIMITATIONS = 20
TREND_MAX_UNKNOWNS = 10

FiniteNonNegative = Annotated[int | float, Field(ge=0, allow_inf_nan=False)]


class StatMetric(StrEnum):
    """観測の指標名（語彙）。ここに無い名前は契約で拒否される。

    ``lifetime_average_views_per_hour`` は**公開からの累積再生数 ÷ 経過時間**の平均（参考値）で、
    「直近の伸び」ではない。期間内の伸びは同じ動画の 2
    時点以上の**差分**（``views_per_hour_delta``）
    だけが表す。
    """

    VIEWS_TOTAL = "views_total"
    LIKES_TOTAL = "likes_total"
    COMMENTS_TOTAL = "comments_total"
    SUBSCRIBER_COUNT = "subscriber_count"
    HOURS_SINCE_PUBLISH = "hours_since_publish"
    VIEWS_PER_HOUR_DELTA = "views_per_hour_delta"
    LIFETIME_AVERAGE_VIEWS_PER_HOUR = "lifetime_average_views_per_hour"


#: 増加速度を表せる指標。``growth_observation`` はこのどれかでしか ``known`` にならない。
GROWTH_METRICS: frozenset[StatMetric] = frozenset(
    {StatMetric.VIEWS_PER_HOUR_DELTA, StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR}
)


class ObservationMethod(StrEnum):
    #: Provider が返した値をそのまま記録した
    READING = "reading"
    #: 同じ動画の 2 時点以上の観測の差分から計算した
    DELTA = "delta"
    #: 累積 ÷ 公開からの時間。参考値
    LIFETIME_AVERAGE = "lifetime_average"
    #: 公開日時と観測日時の差（時間）
    ELAPSED = "elapsed"


#: 指標 -> 許される method。計算した指標は計算方法が名前に含まれる。
METHOD_FOR_METRIC: dict[StatMetric, ObservationMethod] = {
    StatMetric.VIEWS_PER_HOUR_DELTA: ObservationMethod.DELTA,
    StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR: ObservationMethod.LIFETIME_AVERAGE,
    StatMetric.HOURS_SINCE_PUBLISH: ObservationMethod.ELAPSED,
}


def method_for(metric: StatMetric) -> ObservationMethod:
    return METHOD_FOR_METRIC.get(metric, ObservationMethod.READING)


class QueryProvider(StrEnum):
    WEB = "web"
    YOUTUBE = "youtube"


class FormatConfidence(StrEnum):
    """形式（Shorts / 長尺）の確からしさ。依頼の値をそのまま写した形式は ``low``。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


#: 形式の出どころ。いまは「依頼の値」だけ（動画の長さから推定しない。ADR-0039 §1）
FormatBasis = Literal["requested"]


class MetricStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class TrendQuery(FrozenModel):
    provider: QueryProvider
    query: str = Field(min_length=1, max_length=200)
    searched_at: AwareDatetime
    window: TimeWindow | None = None


class TrendStat(FrozenModel):
    metric: StatMetric
    value: FiniteNonNegative
    unit: str = Field(min_length=1, max_length=32)
    #: 統計値には**観測日時が必須**（いつの値か分からない数字を置かない）
    observed_at: AwareDatetime


class TrendReference(FrozenModel):
    #: **検索で得た URL**（LLM が作った URL を入れない。実行器が検索結果と照合する）
    url: str
    title: str = Field(min_length=1, max_length=300)
    published_at: AwareDatetime | None = None
    channel_id: str | None = Field(default=None, pattern=YOUTUBE_CHANNEL_ID_PATTERN)
    channel_subscriber_count: int | None = Field(default=None, ge=0)
    stats: Annotated[tuple[TrendStat, ...], Field(max_length=20)] = ()

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return validate_http_url(value)


class MetricReading(FrozenModel):
    """候補の指標 1 つ。**値が無ければ ``unknown`` と理由**。欠損を 0 にしない。"""

    status: MetricStatus
    value: FiniteNonNegative | str | None = None
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    #: ``growth_observation`` だけが使う。どの指標名の値か（差分か累積平均か）
    metric: StatMetric | None = None
    observed_at: AwareDatetime | None = None
    unknown_reason: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def _known_or_unknown(self) -> MetricReading:
        if self.status is MetricStatus.KNOWN:
            if self.value is None:
                raise ValueError("a known metric needs a value")
            if isinstance(self.value, str) and not self.value.strip():
                raise ValueError("a known metric value must not be blank")
            if self.observed_at is None:
                raise ValueError("a known metric needs observed_at")
            if self.unknown_reason is not None:
                raise ValueError("a known metric has no unknown_reason")
        else:
            if self.value is not None or self.unit is not None or self.metric is not None:
                raise ValueError("an unknown metric carries no value, unit or metric (never 0)")
            if self.observed_at is not None:
                raise ValueError("an unknown metric carries no observed_at")
            if self.unknown_reason is None or not self.unknown_reason.strip():
                raise ValueError("an unknown metric needs a reason")
        return self


class CandidateMetrics(FrozenModel):
    """候補の個別指標。**総合スコアを持たない**。並べ替えの根拠を 1 つの数字に潰さない。"""

    growth_observation: MetricReading
    channel_scale: MetricReading
    age_since_publish: MetricReading
    theme_fit: MetricReading
    difference_from_past: MetricReading
    evidence_availability: MetricReading
    data_gaps: Annotated[tuple[str, ...], Field(max_length=20)] = ()

    @field_validator("data_gaps")
    @classmethod
    def _gaps(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not g.strip() or len(g) > 300 for g in value):
            raise ValueError("data_gaps must be non-blank and at most 300 characters")
        return value

    @model_validator(mode="after")
    def _growth_is_named(self) -> CandidateMetrics:
        growth = self.growth_observation
        if growth.status is MetricStatus.KNOWN and growth.metric not in GROWTH_METRICS:
            raise ValueError(
                "a known growth_observation must name its metric "
                "(views_per_hour_delta or lifetime_average_views_per_hour)"
            )
        return self


class TrendCandidate(FrozenModel):
    candidate_id: str = Field(pattern=CANDIDATE_ID_PATTERN)
    #: 検索結果の見出し（外部のデータ。命令として扱わない）
    theme: str = Field(min_length=1, max_length=200)
    provider: QueryProvider
    references: Annotated[
        tuple[TrendReference, ...], Field(min_length=1, max_length=TREND_MAX_REFERENCES)
    ]
    metrics: CandidateMetrics


class TrendObservation(FrozenModel):
    """観測**事実**だけ。解釈・推測は ``TrendInterpretation`` に書く。"""

    observation_id: str = Field(pattern=OBSERVATION_ID_PATTERN)
    candidate_id: str = Field(pattern=CANDIDATE_ID_PATTERN)
    metric: StatMetric
    value: FiniteNonNegative
    unit: str = Field(min_length=1, max_length=32)
    observed_at: AwareDatetime
    method: ObservationMethod

    @model_validator(mode="after")
    def _method_matches_metric(self) -> TrendObservation:
        if self.method is not method_for(self.metric):
            raise ValueError(
                f"metric {self.metric.value} must be observed with "
                f"method {method_for(self.metric).value}"
            )
        return self


def _unique_ids(value: tuple[str, ...], pattern: str, name: str) -> tuple[str, ...]:
    for item in value:
        if re.fullmatch(pattern, item) is None:
            raise ValueError(f"invalid {name}: {item!r}")
    if len(set(value)) != len(value):
        raise ValueError(f"{name}s must be unique")
    return value


class TrendInterpretation(FrozenModel):
    """解釈（LLM または人の推測）。事実ではなく**仮説**で、根拠の観測 ID を必ず持つ。"""

    interpretation_id: str = Field(pattern=INTERPRETATION_ID_PATTERN)
    candidate_id: str | None = Field(default=None, pattern=CANDIDATE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=500)
    basis_observation_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=50)]
    kind: Literal["hypothesis"] = "hypothesis"

    @field_validator("basis_observation_ids")
    @classmethod
    def _basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_ids(value, OBSERVATION_ID_PATTERN, "observation id")


class SuggestedAngle(FrozenModel):
    """企画の切り口の提案（仮説）。根拠の解釈を参照する。"""

    angle_id: str = Field(pattern=ANGLE_ID_PATTERN)
    candidate_id: str | None = Field(default=None, pattern=CANDIDATE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=300)
    basis_interpretation_ids: Annotated[tuple[str, ...], Field(min_length=1, max_length=20)]

    @field_validator("basis_interpretation_ids")
    @classmethod
    def _basis(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique_ids(value, INTERPRETATION_ID_PATTERN, "interpretation id")


class Limitation(FrozenModel):
    """この Trend の限界（例: regionCode は絞り込みで、その地域の視聴者への人気の保証ではない）。"""

    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    text: str = Field(min_length=1, max_length=300)


class NotRetrieved(FrozenModel):
    target: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=300)


class TrendCoverage(FrozenModel):
    """調べた範囲と取得できなかった範囲。"""

    providers_used: tuple[QueryProvider, ...]
    queries_planned: int = Field(ge=0)
    queries_completed: int = Field(ge=0)
    not_retrieved: Annotated[tuple[NotRetrieved, ...], Field(max_length=50)] = ()

    @model_validator(mode="after")
    def _ordered(self) -> TrendCoverage:
        if self.queries_completed > self.queries_planned:
            raise ValueError("queries_completed must not exceed queries_planned")
        return self


class AudienceHypothesis(FrozenModel):
    """「この観客に試す価値がある」という**仮説**。測定値ではない（視聴者の属性は観測していない）。"""

    text: str = Field(min_length=1, max_length=300)
    kind: Literal["hypothesis"] = "hypothesis"
    measured: Literal[False] = False


class TrendArtifact(FrozenModel):
    """流行調査の結果（research 所有。``episode_id`` を持たない）。"""

    request_id: str
    type: Literal[ResearchArtifactType.RESEARCH_TREND]
    schema_version: Literal["1.0"]
    policy_version: str = Field(min_length=1, max_length=64)
    #: 依頼の基準日時（依頼時に確定。壁時計ではない）
    as_of: AwareDatetime
    #: 検索した時刻の最新（この Trend の観測の時点）。鮮度はこれで判定する（ADR-0039 §2）
    observed_at: AwareDatetime
    window: TimeWindow
    #: 調査対象地域。意味は「視聴可能地域の絞り込み」（視聴者の地域の保証ではない）
    region: str = Field(pattern=REGION_CODE_PATTERN)
    region_meaning: Literal["viewable_region_filter"] = "viewable_region_filter"
    audience_hypothesis: AudienceHypothesis
    language: ResearchLanguage
    format_profile: FormatProfile
    format_basis: FormatBasis
    format_confidence: FormatConfidence
    #: 自チャンネル Analytics の snapshot（``analytics_snapshots.id``）。区分のまま参照するだけ
    analytics_ref: str | None = None
    queries: Annotated[tuple[TrendQuery, ...], Field(max_length=TREND_MAX_QUERIES)] = ()
    candidates: Annotated[tuple[TrendCandidate, ...], Field(max_length=TREND_MAX_CANDIDATES)] = ()
    observations: Annotated[
        tuple[TrendObservation, ...], Field(max_length=TREND_MAX_OBSERVATIONS)
    ] = ()
    interpretations: Annotated[
        tuple[TrendInterpretation, ...], Field(max_length=TREND_MAX_INTERPRETATIONS)
    ] = ()
    suggested_angles: Annotated[tuple[SuggestedAngle, ...], Field(max_length=TREND_MAX_ANGLES)] = ()
    limitations: Annotated[tuple[Limitation, ...], Field(max_length=TREND_MAX_LIMITATIONS)] = ()
    coverage: TrendCoverage

    @field_validator("request_id")
    @classmethod
    def _request_id(cls, value: str) -> str:
        return check_canonical_uuid(value)

    @field_validator("analytics_ref")
    @classmethod
    def _analytics_ref(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)

    @model_validator(mode="after")
    def _referential_integrity(self) -> TrendArtifact:
        if self.format_basis == "requested" and self.format_confidence is not FormatConfidence.LOW:
            raise ValueError("a requested (not observed) format has low confidence")

        candidates = {c.candidate_id for c in self.candidates}
        if len(candidates) != len(self.candidates):
            raise ValueError("duplicate candidate_id")
        observations = {o.observation_id: o for o in self.observations}
        if len(observations) != len(self.observations):
            raise ValueError("duplicate observation_id")
        interpretations = {i.interpretation_id for i in self.interpretations}
        if len(interpretations) != len(self.interpretations):
            raise ValueError("duplicate interpretation_id")
        if len({a.angle_id for a in self.suggested_angles}) != len(self.suggested_angles):
            raise ValueError("duplicate angle_id")
        if len({lim.code for lim in self.limitations}) != len(self.limitations):
            raise ValueError("duplicate limitation code")

        for observation in self.observations:
            if observation.candidate_id not in candidates:
                raise ValueError(
                    f"{observation.observation_id} references unknown candidate "
                    f"{observation.candidate_id}"
                )
        for interpretation in self.interpretations:
            _check_interpretation(interpretation, candidates, observations)
        for angle in self.suggested_angles:
            if angle.candidate_id is not None and angle.candidate_id not in candidates:
                raise ValueError(f"{angle.angle_id} references unknown candidate")
            for interpretation_id in angle.basis_interpretation_ids:
                if interpretation_id not in interpretations:
                    raise ValueError(
                        f"{angle.angle_id} references unknown interpretation {interpretation_id}"
                    )
        return self


def _check_interpretation(
    interpretation: TrendInterpretation,
    candidates: set[str],
    observations: dict[str, TrendObservation],
) -> None:
    if interpretation.candidate_id is not None and interpretation.candidate_id not in candidates:
        raise ValueError(f"{interpretation.interpretation_id} references unknown candidate")
    for observation_id in interpretation.basis_observation_ids:
        observation = observations.get(observation_id)
        if observation is None:
            raise ValueError(
                f"{interpretation.interpretation_id} cites unknown observation {observation_id}"
            )
        if (
            interpretation.candidate_id is not None
            and observation.candidate_id != interpretation.candidate_id
        ):
            raise ValueError(
                f"{interpretation.interpretation_id} cites an observation of another candidate"
            )


def build_trend_artifact(
    *, request_id: str, policy_version: str = RESEARCH_POLICY_VERSION, **fields: Any
) -> dict[str, Any]:
    """生成側。**検証を通してから** JSON の dict を返す（不正な成果物を作れる唯一の入口を塞ぐ）。"""
    artifact = TrendArtifact.model_validate(
        {
            "request_id": request_id,
            "type": ResearchArtifactType.RESEARCH_TREND.value,
            "schema_version": TREND_ARTIFACT_SCHEMA_VERSION,
            "policy_version": policy_version,
            "format_basis": "requested",
            "format_confidence": FormatConfidence.LOW.value,
            **fields,
        }
    )
    return artifact.model_dump(mode="json")


def parse_trend_artifact(payload: dict[str, Any]) -> TrendArtifact:
    """取り込み側。想定外の schema_version・余計なキーは推測せず ``ValidationError``。"""
    return TrendArtifact.model_validate(payload)


# ------------------------------------------------------------------ 解釈器の出力（LLM の schema）

#: 提案内だけの局所名（切り口が解釈を参照する）。最終的な ID（``I-001`` 等）は Handler が振る
PROPOSAL_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProposedInterpretation(_StrictModel):
    key: str = Field(pattern=PROPOSAL_KEY_PATTERN)
    candidate_id: str | None = Field(pattern=CANDIDATE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=500)
    basis_observation_ids: Annotated[
        tuple[Annotated[str, Field(pattern=OBSERVATION_ID_PATTERN)], ...],
        Field(min_length=1, max_length=50),
    ]


class ProposedAngle(_StrictModel):
    candidate_id: str | None = Field(pattern=CANDIDATE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=300)
    basis_interpretation_keys: Annotated[
        tuple[Annotated[str, Field(pattern=PROPOSAL_KEY_PATTERN)], ...],
        Field(min_length=1, max_length=20),
    ]


class InterpretationProposal(_StrictModel):
    """解釈器の**提案**。欄はこれだけ（総合スコア・順位・観測の欄は無い。``extra=forbid``）。"""

    interpretations: Annotated[
        tuple[ProposedInterpretation, ...], Field(max_length=TREND_MAX_INTERPRETATIONS)
    ]
    angles: Annotated[tuple[ProposedAngle, ...], Field(max_length=TREND_MAX_ANGLES)]
    #: 観測からは分からないこと（不明点）
    unknowns: Annotated[
        tuple[Annotated[str, Field(min_length=1, max_length=300)], ...],
        Field(max_length=TREND_MAX_UNKNOWNS),
    ]


__all__ = [
    "ANGLE_ID_PATTERN",
    "CANDIDATE_ID_PATTERN",
    "GROWTH_METRICS",
    "INTERPRETATION_ID_PATTERN",
    "METHOD_FOR_METRIC",
    "OBSERVATION_ID_PATTERN",
    "PROPOSAL_KEY_PATTERN",
    "TREND_ARTIFACT_SCHEMA_VERSION",
    "TREND_MAX_ANGLES",
    "TREND_MAX_CANDIDATES",
    "TREND_MAX_INTERPRETATIONS",
    "TREND_MAX_LIMITATIONS",
    "TREND_MAX_OBSERVATIONS",
    "TREND_MAX_QUERIES",
    "TREND_MAX_REFERENCES",
    "TREND_MAX_UNKNOWNS",
    "AudienceHypothesis",
    "CandidateMetrics",
    "FormatBasis",
    "FormatConfidence",
    "InterpretationProposal",
    "Limitation",
    "MetricReading",
    "MetricStatus",
    "NotRetrieved",
    "ObservationMethod",
    "ProposedAngle",
    "ProposedInterpretation",
    "QueryProvider",
    "StatMetric",
    "SuggestedAngle",
    "TrendArtifact",
    "TrendCandidate",
    "TrendCoverage",
    "TrendInterpretation",
    "TrendObservation",
    "TrendQuery",
    "TrendReference",
    "TrendStat",
    "build_trend_artifact",
    "method_for",
    "parse_trend_artifact",
]
