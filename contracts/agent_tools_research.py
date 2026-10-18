"""Research tool の入出力 model と ToolDef（ADR-0047 §4）。唯一の宣言元（INV-62）。

出力は**要約**だけ: ID・評価・メタデータ・出典の参照。外部ページの抜粋（``excerpt``）と本文は
入れない。外部由来の文字列（見出し・理由）は ``ToolEnvelope.untrusted`` の下にあり、指示として
扱わない（INV-60）。数値の信頼度は作らない（評価は Research の ``assessment`` をそのまま通す）。
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.agent_tools import ToolDef, ToolId
from contracts.artifact_refs import check_canonical_uuid
from contracts.research import (
    MAX_CLAIMS_PER_REQUEST,
    MAX_TREND_SEED_TERMS,
    REGION_CODE_PATTERN,
    SEED_TERM_MAX_CHARS,
    ClaimInput,
    FormatProfile,
    ResearchLanguage,
    normalize_claim_text,
)

__all__ = [
    "FETCH_SOURCES_DEF",
    "SEARCH_HISTORICAL_FACTS_DEF",
    "SEARCH_TRENDS_DEF",
    "RESEARCH_TOOL_DEFS",
    "FetchSourcesInput",
    "FetchSourcesOutput",
    "SearchHistoricalFactsInput",
    "SearchHistoricalFactsOutput",
    "SearchTrendsInput",
    "SearchTrendsOutput",
]

#: 窓の日数の上限（Trend の ``time_window`` は ctx.now から決める）
MAX_TREND_WINDOW_DAYS = 90
MAX_SOURCES_RETURNED = 50


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ------------------------------------------------------------------ search_trends


class SearchTrendsInput(_M):
    #: 視聴可能地域の絞り込み（ISO 3166-1 alpha-2）。「その地域の視聴者への人気」の保証ではない
    region: str = Field(pattern=REGION_CODE_PATTERN)
    #: 「この観客に試す価値がある」という**仮説**（実測ではない）
    audience_hypothesis: str = Field(min_length=1, max_length=300)
    seed_terms: Annotated[tuple[str, ...], Field(max_length=MAX_TREND_SEED_TERMS)] = ()
    language: ResearchLanguage = "ja"
    format_profile: FormatProfile = "any"
    window_days: int = Field(default=30, ge=1, le=MAX_TREND_WINDOW_DAYS)

    @field_validator("seed_terms")
    @classmethod
    def _seed_terms(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [normalize_claim_text(t) for t in value]
        if any(not t for t in normalized) or any(len(t) > SEED_TERM_MAX_CHARS for t in value):
            raise ValueError("seed_terms must be non-blank and at most 100 characters")
        if len(set(normalized)) != len(normalized):
            raise ValueError("seed_terms must be unique after normalization")
        return value


class SourceRef(_M):
    url: str
    title: str
    published_at: str | None = None


class MetricView(_M):
    #: ``known`` / ``unknown``。欠損を 0 にしない
    status: str
    value: float | str | None = None
    unit: str | None = None
    metric: str | None = None
    observed_at: str | None = None
    unknown_reason: str | None = None


class TrendCandidateView(_M):
    candidate_id: str
    theme: str
    sources: tuple[SourceRef, ...]
    growth_observation: MetricView
    age_since_publish: MetricView
    data_gaps: tuple[str, ...] = ()


class HypothesisView(_M):
    id: str
    text: str
    candidate_id: str | None = None
    #: 常に仮説（観測ではない）
    kind: Literal["hypothesis"] = "hypothesis"


class AudienceHypothesisView(_M):
    text: str
    kind: Literal["hypothesis"] = "hypothesis"
    measured: Literal[False] = False


class CoverageView(_M):
    items_requested: int
    items_covered: int
    not_covered: tuple[str, ...] = ()


class LimitationView(_M):
    code: str
    text: str


class SearchTrendsOutput(_M):
    research_request_id: str
    artifact_id: str
    observed_at: str
    region: str
    region_meaning: Literal["viewable_region_filter"] = "viewable_region_filter"
    audience_hypothesis: AudienceHypothesisView
    candidates: tuple[TrendCandidateView, ...]
    interpretations: tuple[HypothesisView, ...] = ()
    suggested_angles: tuple[HypothesisView, ...] = ()
    coverage: CoverageView
    limitations: tuple[LimitationView, ...] = ()


# ------------------------------------------------------------------ search_historical_facts


class SearchHistoricalFactsInput(_M):
    claims: Annotated[
        tuple[ClaimInput, ...], Field(min_length=1, max_length=MAX_CLAIMS_PER_REQUEST)
    ]
    language: ResearchLanguage = "ja"

    @model_validator(mode="after")
    def _unique(self) -> SearchHistoricalFactsInput:
        keys = [normalize_claim_text(c.claim_text) for c in self.claims]
        if len(set(keys)) != len(keys):
            raise ValueError("claim_text must be unique after normalization")
        return self


class ClaimLink(_M):
    source_id: str
    stance: str


class ClaimAssessmentView(_M):
    claim_id: str
    claim_text: str
    kind: str
    importance: str
    strong: bool
    #: 評価器が評価したか。偽は ``insufficient`` だけ
    assessed: bool
    #: Research の ``supported`` / ``qualified`` / ``disputed`` / ``insufficient`` をそのまま
    assessment: str
    assessment_reason: str
    source_ids: tuple[str, ...] = ()
    links: tuple[ClaimLink, ...] = ()
    unverified_points: tuple[str, ...] = ()


class GapView(_M):
    claim_id: str | None = None
    description: str
    suggested_query: str | None = None


class SearchHistoricalFactsOutput(_M):
    research_request_id: str
    artifact_id: str
    observed_at: str
    claims: tuple[ClaimAssessmentView, ...]
    gaps: tuple[GapView, ...] = ()
    coverage: CoverageView
    limitations: tuple[LimitationView, ...] = ()


# ------------------------------------------------------------------ fetch_sources


class FetchSourcesInput(_M):
    """既存の Research 依頼の資料一覧を見る。任意 URL の取得は**できない**（``not_supported``）。"""

    research_request_id: str | None = None
    #: 受け取って ``not_supported`` を返すためだけの欄（取得はしない）
    url: str | None = Field(default=None, max_length=2048)

    @field_validator("research_request_id")
    @classmethod
    def _rid(cls, value: str | None) -> str | None:
        return None if value is None else check_canonical_uuid(value)

    @model_validator(mode="after")
    def _one(self) -> FetchSourcesInput:
        if self.research_request_id is None and self.url is None:
            raise ValueError("research_request_id is required")
        if self.url is not None and re.search(r"\s", self.url):
            raise ValueError("url must not contain whitespace")
        return self


class SourceView(_M):
    source_id: str
    url: str
    title: str
    language: str
    published_at: str | None = None
    retrieved_at: str
    content_sha256: str | None = None
    fetch_status: str
    source_kind: str


class FetchSourcesOutput(_M):
    research_request_id: str
    artifact_id: str
    sources: tuple[SourceView, ...]
    limitations: tuple[LimitationView, ...] = ()


# ------------------------------------------------------------------ 定義

SEARCH_TRENDS_DEF = ToolDef(
    tool_id=ToolId.SEARCH_TRENDS,
    version=1,
    description=(
        "Search current YouTube/web trends for a region. Returns observed candidates "
        "and hypotheses (never measured audience facts). May return 'pending' while the "
        "research is still running; call again with the same input to resume."
    ),
    input_model=SearchTrendsInput,
    output_model=SearchTrendsOutput,
    timeout_seconds=90,
    max_calls_per_run=2,
    cost_class="research_ledger",
)
SEARCH_HISTORICAL_FACTS_DEF = ToolDef(
    tool_id=ToolId.SEARCH_HISTORICAL_FACTS,
    version=1,
    description=(
        "Check up to 12 historical claims against sources. Returns the research "
        "assessment per claim (supported/qualified/disputed/insufficient) with source ids "
        "and gaps. No numeric confidence."
    ),
    input_model=SearchHistoricalFactsInput,
    output_model=SearchHistoricalFactsOutput,
    timeout_seconds=90,
    max_calls_per_run=2,
    cost_class="research_ledger",
)
FETCH_SOURCES_DEF = ToolDef(
    tool_id=ToolId.FETCH_SOURCES,
    version=1,
    description=(
        "List the sources recorded for an existing research request (url, title, dates, "
        "hash, fetch status). Cannot fetch arbitrary URLs."
    ),
    input_model=FetchSourcesInput,
    output_model=FetchSourcesOutput,
    timeout_seconds=15,
    max_calls_per_run=5,
    cost_class="research_ledger",
)

RESEARCH_TOOL_DEFS: tuple[ToolDef, ...] = (
    SEARCH_TRENDS_DEF,
    SEARCH_HISTORICAL_FACTS_DEF,
    FETCH_SOURCES_DEF,
)
