"""``TrendHandler``: Trend 種別の判断（ADR-0039 §1 / §3）。純粋・決定的（INV-6）。

共通の骨格（予約・予算・検索・解釈の呼び出し・生データ・成果物）は実行器が持つ。ここは:

- ``plan_searches``: ``trend_planning`` の決定的な計画（YouTube と Web、7 日と 30 日の窓）
- ``select_fetches``: **空**（Trend は本文を取得しない。統計は検索結果にある）
- ``plan_interpretation``: 検索結果から観測を組み、解釈 1 回を計画する
  （観測が無ければ解釈器を呼ばない）
- ``synthesize``: 同じ観測を組み直し、実行器が台帳を通して得た解釈の**提案**を検査して
  採用し、``TrendArtifact`` を契約の validator を通して組む

柱:

- **観測と解釈は別の欄**。観測は Provider が返した値と、そこから計算した値
  （差分・参考の平均・経過時間）だけで、どれも ``observed_at`` を持つ
- **単一の総合スコア・順位を作らない**。指標は個別。値が取れなければ ``unknown`` と理由
  （0 にしない）
- 増加速度: 同じ動画を 2 時点以上観測できたら ``views_per_hour_delta``（差分）、
  1 時点だけなら ``lifetime_average_views_per_hour``（参考値。限界に明記）
- 解釈の提案は**信用しない**: 存在しない観測 ID・別の候補の観測・総合スコアや順位の主張・
  参考値を「直近の伸び」と呼ぶ文・存在しない解釈を指す切り口のどれかがあれば
  **提案ごと採用しない**（修復しない）。その Trend は観測だけの ``partial``
- 参照 URL は**検索で得た URL だけ**（実行器が検索結果と照合する）
- ``region`` / ``language`` は絞り込み。動画の長さから Shorts と断定しない
  （形式は依頼の値・確からしさ low）

完了の判断: 計画した検索がすべて結果を返し、統計の欠け・候補の上限超え・検索結果の警告が無く、
観測があれば解釈を採用できたときだけ ``complete``。それ以外は ``partial``
（呼び出し側は「Trend 無し」として扱う）。
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from contracts.research import (
    RESEARCH_POLICY_VERSION,
    ResearchArtifactType,
    ResearchCoverage,
    ResearchKind,
    TrendResearchRequest,
    normalize_claim_text,
)
from contracts.research_evidence import validate_http_url
from contracts.research_trend import (
    TREND_ARTIFACT_SCHEMA_VERSION,
    TREND_MAX_CANDIDATES,
    TREND_MAX_LIMITATIONS,
    TREND_MAX_QUERIES,
    CandidateMetrics,
    InterpretationProposal,
    Limitation,
    MetricReading,
    MetricStatus,
    NotRetrieved,
    ObservationMethod,
    QueryProvider,
    StatMetric,
    SuggestedAngle,
    TrendCandidate,
    TrendCoverage,
    TrendInterpretation,
    TrendObservation,
    TrendQuery,
    TrendReference,
    TrendStat,
    build_trend_artifact,
    method_for,
)
from contracts.upload import YOUTUBE_CHANNEL_ID_PATTERN
from domain.research.errors import ResearchInputInvalidError
from domain.research.evidence_text import clip
from domain.research.handlers import (
    FetchedSource,
    FetchTarget,
    HandlerOutput,
    InterpretationTask,
    ResearchSpec,
    SearchRound,
    SearchStep,
    SynthesisContext,
)
from domain.research.ports import SearchHit
from domain.research.trend_metrics import (
    ViewObservation,
    lifetime_average_views_per_hour,
    views_per_hour_delta,
)
from domain.research.trend_planning import plan_trend_searches
from domain.research.trend_ports import CandidateFact, InterpretContext, ObservationFact
from domain.research.urls import normalize_url

__all__ = [
    "AUTHORITATIVE_HOST_SUFFIXES",
    "INTERPRETATION_TASK_ID",
    "MIN_SAMPLE_CANDIDATES",
    "InterpretationRejectedError",
    "TrendFacts",
    "TrendHandler",
    "adopt_interpretation",
    "build_trend_facts",
    "format_graded",
]

#: 解釈 1 回の ID（依頼の中で一意・決定的）
INTERPRETATION_TASK_ID = "trend-interpret"

#: YouTube 候補がこれ未満なら「サンプル不足」を限界に書く（初期版の単純な閾値）
MIN_SAMPLE_CANDIDATES = 5

#: 権威ある種類（公的機関・学術）の可能性が高いホストの接尾辞。決定的な**簡易**指標で、資料の質の
#: 保証ではない（資料の評価は Evidence の別工程。ADR-0038）
AUTHORITATIVE_HOST_SUFFIXES: tuple[str, ...] = (
    ".go.jp",
    ".lg.jp",
    ".ac.jp",
    ".ed.jp",
    ".gov",
    ".edu",
    ".museum",
)

#: Port の指標名 → 契約の語彙と単位
_STAT_MAP: dict[str, tuple[StatMetric, str]] = {
    "view_count": (StatMetric.VIEWS_TOTAL, "views"),
    "like_count": (StatMetric.LIKES_TOTAL, "likes"),
    "comment_count": (StatMetric.COMMENTS_TOTAL, "comments"),
}

_SCORE_CLAIM = re.compile(
    r"総合(?:スコア|評価|順位|点)|overall[\s_-]*(?:score|rank)"
    r"|(?:スコア|score)\s*[:：=]?\s*\d|(?:順位|rank)\s*[:：=]?\s*\d|\d+\s*位",
    re.IGNORECASE,
)
_RECENT_GROWTH_CLAIM = re.compile(r"直近の伸び|recent[\s_-]*growth", re.IGNORECASE)

_LEVELS = ("high", "medium", "low")
_SECONDS_PER_HOUR = 3600.0


class InterpretationRejectedError(ValueError):
    """解釈器の提案を採用しない理由（提案ごと捨てる。修復しない）。"""


def format_graded(level: str, basis: Sequence[str]) -> str:
    """段階（high / medium / low）と根拠語を 1 つの文字列にする（``"<level>: <根拠>"``）。"""
    if level not in _LEVELS:
        raise ValueError(f"unknown level: {level!r}")
    return f"{level}: {'、'.join(basis)}"[:280]


def _unknown(reason: str) -> MetricReading:
    return MetricReading(status=MetricStatus.UNKNOWN, unknown_reason=clip(reason, 300))


def _known_text(value: str, observed_at: datetime) -> MetricReading:
    return MetricReading(status=MetricStatus.KNOWN, value=value, observed_at=observed_at)


def _finite_nonneg(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _matched_terms(text: str, terms: Sequence[str]) -> tuple[str, ...]:
    haystack = normalize_claim_text(text)
    matched: list[str] = []
    for term in terms:
        words = normalize_claim_text(term).split()
        if words and all(word in haystack for word in words):
            matched.append(term)
    return tuple(matched)


def _is_authoritative_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(
        host.endswith(suffix) or host == suffix[1:] for suffix in AUTHORITATIVE_HOST_SUFFIXES
    )


def _request(spec: ResearchSpec) -> TrendResearchRequest:
    if not isinstance(spec, TrendResearchRequest):
        raise ResearchInputInvalidError("TrendHandler needs a trend research request")
    return spec


# ------------------------------------------------------------------ 観測（事実）を組む


@dataclass(slots=True)
class _Item:
    """検索で見つけた 1 件（同じ URL は 1 つ。見出し・統計は最も新しい検索結果のもの）。"""

    hit: SearchHit
    provider: QueryProvider
    searched_at: datetime
    #: 同じ動画の累積再生数の観測（検索のたびに 1 つ。時刻の重複を含みうる）
    views: list[ViewObservation] = field(default_factory=list)
    candidate_id: str = ""
    matched: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrendFacts:
    """検索結果から決定的に組んだ観測（``plan_interpretation`` と ``synthesize`` が同じものを使う）
    。"""

    items: tuple[_Item, ...]
    candidates: tuple[TrendCandidate, ...]
    observations: tuple[TrendObservation, ...]
    dropped_urls: int
    over_cap: int
    missing_views: bool
    uses_lifetime_average: bool
    observed_at: datetime


class _Observations:
    def __init__(self) -> None:
        self.rows: list[TrendObservation] = []

    def add(
        self,
        candidate_id: str,
        metric: StatMetric,
        value: int | float,
        unit: str,
        observed_at: datetime,
    ) -> None:
        self.rows.append(
            TrendObservation(
                observation_id=f"O-{len(self.rows) + 1:03d}",
                candidate_id=candidate_id,
                metric=metric,
                value=value,
                unit=unit,
                observed_at=observed_at,
                method=method_for(metric),
            )
        )


def _collect(rounds: Sequence[SearchRound]) -> tuple[list[_Item], int, int]:
    """(候補, URL が不正で落とした hit の数, 上限を超えて記録しない候補の数)。出現順・決定的。"""
    found: dict[str, _Item] = {}
    dropped = 0
    for search_round in rounds:
        results = search_round.results
        if results is None:
            continue
        for hit in results.hits:
            try:
                validate_http_url(hit.url)
            except ValueError:
                dropped += 1
                continue
            key = normalize_url(hit.url)
            item = found.get(key)
            if item is None:
                item = _Item(
                    hit=hit,
                    provider=QueryProvider(search_round.step.query.kind),
                    searched_at=results.searched_at,
                )
                found[key] = item
            elif results.searched_at > item.searched_at:
                item.hit, item.searched_at = hit, results.searched_at
            for stat in hit.stats:
                if stat.metric == "view_count" and isinstance(stat.value, int):
                    item.views.append(ViewObservation(stat.value, stat.observed_at))
    items = list(found.values())
    over_cap = max(0, len(items) - TREND_MAX_CANDIDATES)
    return items[:TREND_MAX_CANDIDATES], dropped, over_cap


def _stats_of(hit: SearchHit) -> dict[StatMetric, TrendStat]:
    latest: dict[StatMetric, TrendStat] = {}
    for stat in hit.stats:
        mapped = _STAT_MAP.get(stat.metric)
        if mapped is None or not _finite_nonneg(stat.value):
            continue
        metric, unit = mapped
        current = latest.get(metric)
        if current is None or stat.observed_at > current.observed_at:
            latest[metric] = TrendStat(
                metric=metric, value=stat.value, unit=unit, observed_at=stat.observed_at
            )
    return latest


def _distinct_views(views: Sequence[ViewObservation]) -> list[ViewObservation]:
    by_time: dict[datetime, ViewObservation] = {}
    for view in views:
        by_time.setdefault(view.observed_at, view)
    return [by_time[t] for t in sorted(by_time)]


def _growth(item: _Item) -> MetricReading:
    if item.provider is not QueryProvider.YOUTUBE:
        return _unknown("a web page has no view statistics")
    distinct = _distinct_views(item.views)
    if not distinct:
        return _unknown("view count was not observed")
    if len({v.observed_at for v in item.views}) >= 2:
        return views_per_hour_delta(item.views)
    latest = distinct[-1]
    if item.hit.published_at is None:
        return _unknown("published_at is missing, so the lifetime average cannot be computed")
    return lifetime_average_views_per_hour(latest.views, item.hit.published_at, latest.observed_at)


def _age(published_at: datetime | None, at: datetime) -> MetricReading:
    if published_at is None:
        return _unknown("published_at is missing")
    hours = (at - published_at).total_seconds() / _SECONDS_PER_HOUR
    if hours <= 0:
        return _unknown("observed_at is not later than published_at")
    return MetricReading(status=MetricStatus.KNOWN, value=hours, unit="hours", observed_at=at)


def _theme_fit(item: _Item, seeds: Sequence[str], observed_at: datetime) -> MetricReading:
    if not seeds:
        return _unknown("the request has no seed terms to compare against")
    if not item.matched:
        return _known_text(
            format_graded("low", (f"no seed term in the title or snippet (0/{len(seeds)})",)),
            observed_at,
        )
    level = "high" if len(item.matched) / len(seeds) >= 0.5 else "medium"
    return _known_text(format_graded(level, item.matched), observed_at)


def _difference_from_past(
    item: _Item, past_video_refs: Sequence[str], observed_at: datetime
) -> MetricReading:
    """過去に投稿した動画（``past_video_refs``）との違い。いまは**動画の同一性だけ**を見る。"""
    if not past_video_refs:
        return _unknown("no past videos were provided")
    if item.provider is not QueryProvider.YOUTUBE or not item.hit.provider_ref:
        return _unknown("only a YouTube video can be compared with past videos")
    if item.hit.provider_ref in past_video_refs:
        return _known_text(format_graded("low", ("this is one of the past videos",)), observed_at)
    return _known_text(
        format_graded(
            "high",
            (f"not one of {len(past_video_refs)} past video(s) (identity only, not topic)",),
        ),
        observed_at,
    )


def _evidence_availability(
    item: _Item, items: Sequence[_Item], observed_at: datetime
) -> MetricReading:
    """同じテーマの Web ページに権威ある種類のホストがあるか（簡易・決定的）。"""
    related = [
        other
        for other in items
        if other.provider is QueryProvider.WEB
        and (other is item or (set(other.matched) & set(item.matched)))
    ]
    if not related:
        return _unknown("no web page was found for this theme")
    hosts = [
        (urlsplit(o.hit.url).hostname or "") for o in related if _is_authoritative_host(o.hit.url)
    ]
    if len(hosts) >= 2:
        return _known_text(format_graded("high", hosts[:3]), observed_at)
    if hosts:
        return _known_text(format_graded("medium", hosts), observed_at)
    return _known_text(
        format_graded("low", (f"no authoritative host among {len(related)} web page(s)",)),
        observed_at,
    )


def _candidate(
    item: _Item,
    items: Sequence[_Item],
    request: TrendResearchRequest,
    observed_at: datetime,
    build: _Observations,
) -> tuple[TrendCandidate, bool, bool]:
    """(候補, 再生数を観測できた, 増加速度が参考の平均)。観測（事実）を ``build`` に足す。"""
    hit, cid = item.hit, item.candidate_id
    gaps: list[str] = []
    youtube = item.provider is QueryProvider.YOUTUBE
    stats = _stats_of(hit) if youtube else {}

    channel_id = hit.channel_id
    if channel_id is not None and re.fullmatch(YOUTUBE_CHANNEL_ID_PATTERN, channel_id) is None:
        channel_id = None
        gaps.append("channel_id was not a valid YouTube channel id and was dropped")
    subscribers = hit.channel_subscriber_count if youtube else None
    if subscribers is not None and not _finite_nonneg(subscribers):
        subscribers = None

    title = hit.title.strip() or "(untitled)"
    reference = TrendReference(
        url=hit.url,
        title=title[:300],
        published_at=hit.published_at,
        channel_id=channel_id,
        channel_subscriber_count=subscribers,
        stats=tuple(stats.values()),
    )

    # 観測（事実）。再生数は最も古い観測と最も新しい観測（差分の材料）だけを記録する
    views = _distinct_views(item.views)
    for view in (views[0], views[-1]) if len(views) >= 2 else views:
        assert view.views is not None
        build.add(cid, StatMetric.VIEWS_TOTAL, view.views, "views", view.observed_at)
    for metric in (StatMetric.LIKES_TOTAL, StatMetric.COMMENTS_TOTAL):
        stat = stats.get(metric)
        if stat is not None:
            build.add(cid, metric, stat.value, stat.unit, stat.observed_at)
    if subscribers is not None:
        build.add(cid, StatMetric.SUBSCRIBER_COUNT, subscribers, "subscribers", item.searched_at)
    age_at = views[-1].observed_at if views else item.searched_at
    age = _age(hit.published_at, age_at)
    if age.status is MetricStatus.KNOWN:
        assert isinstance(age.value, int | float)
        build.add(cid, StatMetric.HOURS_SINCE_PUBLISH, age.value, "hours", age_at)
    growth = _growth(item)
    if growth.status is MetricStatus.KNOWN:
        assert growth.metric is not None and isinstance(growth.value, int | float)
        assert growth.unit is not None and growth.observed_at is not None
        build.add(cid, growth.metric, growth.value, growth.unit, growth.observed_at)

    if subscribers is not None:
        scale = MetricReading(
            status=MetricStatus.KNOWN,
            value=subscribers,
            unit="subscribers",
            observed_at=item.searched_at,
        )
    elif youtube:
        scale = _unknown("subscriber count is hidden or was not retrieved")
    else:
        scale = _unknown("a web page has no channel subscriber count")

    for name, reading in (("growth_observation", growth), ("channel_scale", scale), ("age", age)):
        if reading.status is MetricStatus.UNKNOWN and reading.unknown_reason:
            gaps.append(f"{name}: {reading.unknown_reason}")
    metrics = CandidateMetrics(
        growth_observation=growth,
        channel_scale=scale,
        age_since_publish=age,
        theme_fit=_theme_fit(item, tuple(request.inputs.seed_terms), observed_at),
        difference_from_past=_difference_from_past(
            item, request.inputs.past_video_refs, observed_at
        ),
        evidence_availability=_evidence_availability(item, items, observed_at),
        data_gaps=tuple(clip(g, 300) for g in gaps[:20]),
    )
    candidate = TrendCandidate(
        candidate_id=cid,
        theme=title[:200],
        provider=item.provider,
        references=(reference,),
        metrics=metrics,
    )
    lifetime = (
        growth.status is MetricStatus.KNOWN
        and growth.metric is StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR
    )
    return candidate, bool(views), lifetime


def build_trend_facts(request: TrendResearchRequest, rounds: Sequence[SearchRound]) -> TrendFacts:
    """検索結果 → 候補と観測（決定的）。"""
    items, dropped, over_cap = _collect(rounds)
    searched = [r.results.searched_at for r in rounds if r.results is not None]
    observed_at = max(searched) if searched else request.as_of
    seeds = tuple(request.inputs.seed_terms)
    for index, item in enumerate(items, start=1):
        item.candidate_id = f"T-{index:03d}"
        item.matched = _matched_terms(f"{item.hit.title} {item.hit.snippet}", seeds)

    build = _Observations()
    candidates: list[TrendCandidate] = []
    missing_views = False
    uses_lifetime = False
    for item in items:
        candidate, has_views, lifetime = _candidate(item, items, request, observed_at, build)
        candidates.append(candidate)
        missing_views = missing_views or (item.provider is QueryProvider.YOUTUBE and not has_views)
        uses_lifetime = uses_lifetime or lifetime
    return TrendFacts(
        items=tuple(items),
        candidates=tuple(candidates),
        observations=tuple(build.rows),
        dropped_urls=dropped,
        over_cap=over_cap,
        missing_views=missing_views,
        uses_lifetime_average=uses_lifetime,
        observed_at=observed_at,
    )


# ------------------------------------------------------------------ 解釈の採用（検査済み）


def _reject_claims(text: str) -> None:
    if _SCORE_CLAIM.search(text):
        raise InterpretationRejectedError(
            "claims an overall score or a rank (a single score is not allowed)"
        )


def adopt_interpretation(
    proposal: InterpretationProposal,
    observations: Sequence[TrendObservation],
    candidate_ids: Collection[str],
) -> tuple[tuple[TrendInterpretation, ...], tuple[SuggestedAngle, ...], tuple[str, ...]]:
    """解釈器の提案を検査して契約の型にする。**修復しない**（違反は
    ``InterpretationRejectedError``）。"""
    by_id = {o.observation_id: o for o in observations}
    keys = [i.key for i in proposal.interpretations]
    if len(set(keys)) != len(keys):
        raise InterpretationRejectedError("duplicate interpretation key")

    ids: dict[str, str] = {}
    adopted: list[TrendInterpretation] = []
    for index, proposed in enumerate(proposal.interpretations, start=1):
        _reject_claims(proposed.text)
        cited = proposed.basis_observation_ids
        if len(set(cited)) != len(cited):
            raise InterpretationRejectedError("duplicate observation id in basis")
        missing = [o for o in cited if o not in by_id]
        if missing:
            raise InterpretationRejectedError(f"cites unknown observation ids {missing}")
        if proposed.candidate_id is not None:
            if proposed.candidate_id not in candidate_ids:
                raise InterpretationRejectedError(f"unknown candidate {proposed.candidate_id}")
            if any(by_id[o].candidate_id != proposed.candidate_id for o in cited):
                raise InterpretationRejectedError("cites an observation of another candidate")
        if _RECENT_GROWTH_CLAIM.search(proposed.text) and any(
            by_id[o].method is ObservationMethod.LIFETIME_AVERAGE for o in cited
        ):
            raise InterpretationRejectedError("presents a lifetime average as recent growth")
        interpretation_id = f"I-{index:03d}"
        ids[proposed.key] = interpretation_id
        adopted.append(
            TrendInterpretation(
                interpretation_id=interpretation_id,
                candidate_id=proposed.candidate_id,
                text=proposed.text,
                basis_observation_ids=cited,
            )
        )

    angles: list[SuggestedAngle] = []
    for index, angle in enumerate(proposal.angles, start=1):
        _reject_claims(angle.text)
        if angle.candidate_id is not None and angle.candidate_id not in candidate_ids:
            raise InterpretationRejectedError(f"unknown candidate {angle.candidate_id}")
        unknown_keys = [k for k in angle.basis_interpretation_keys if k not in ids]
        if unknown_keys:
            raise InterpretationRejectedError(
                f"angle cites unknown interpretation keys {unknown_keys}"
            )
        angles.append(
            SuggestedAngle(
                angle_id=f"A-{index:03d}",
                candidate_id=angle.candidate_id,
                text=angle.text,
                basis_interpretation_ids=tuple(
                    dict.fromkeys(ids[k] for k in angle.basis_interpretation_keys)
                ),
            )
        )
    for text in proposal.unknowns:
        _reject_claims(text)
    return tuple(adopted), tuple(angles), proposal.unknowns


# ------------------------------------------------------------------ 成果物の周辺


def _trend_query(search_round: SearchRound) -> TrendQuery:
    results = search_round.results
    assert results is not None
    query = search_round.step.query
    window = None
    if (
        query.published_after is not None
        and query.published_before is not None
        and query.published_before > query.published_after
    ):
        window = {"start": query.published_after, "end": query.published_before}
    return TrendQuery.model_validate(
        {
            "provider": QueryProvider(query.kind),
            "query": query.text[:200],
            "searched_at": results.searched_at,
            "window": window,
        }
    )


def _coverage(
    rounds: Sequence[SearchRound], ctx: SynthesisContext, facts: TrendFacts
) -> tuple[TrendCoverage, list[NotRetrieved], bool]:
    """(範囲, 取得できなかったもの, 欠けがある)。"""
    completed = [r for r in rounds if r.results is not None]
    not_retrieved: list[NotRetrieved] = []
    gap = False

    def add(target: str, reason: str) -> None:
        not_retrieved.append(NotRetrieved(target=clip(target, 200), reason=clip(reason, 300)))

    for search_round in rounds:
        query_text = search_round.step.query.text or "search"
        if search_round.results is None:
            gap = True
            add(query_text, f"search failed: {search_round.error or 'unknown'}")
        else:
            for warning in search_round.results.warnings:
                gap = True
                add(query_text, f"partial result: {warning}")
    for skipped in ctx.searches_skipped:
        gap = True
        add(skipped or "search", "skipped: search budget, deadline or stop")
    for failure in ctx.failures:
        gap = True
        add("research", failure or "failure")
    if facts.missing_views:
        gap = True
        add("youtube statistics", "view count was not retrieved for some videos")
    if facts.dropped_urls:
        add("search results", f"{facts.dropped_urls} hit(s) with an invalid url were dropped")
    if facts.over_cap:
        gap = True
        add("candidates", f"{facts.over_cap} candidate(s) beyond the artifact limit")
    if len(completed) > TREND_MAX_QUERIES:
        add("queries", "queries beyond the artifact limit were not recorded")

    providers = tuple(dict.fromkeys(QueryProvider(r.step.query.kind) for r in completed))
    coverage = TrendCoverage(
        providers_used=providers,
        queries_planned=len(rounds) + len(ctx.searches_skipped),
        queries_completed=len(completed),
        not_retrieved=tuple(not_retrieved[:50]),
    )
    return coverage, not_retrieved, gap


def _limitations(facts: TrendFacts, unknowns: Sequence[str]) -> tuple[Limitation, ...]:
    youtube = sum(1 for i in facts.items if i.provider is QueryProvider.YOUTUBE)
    fixed = [
        Limitation(
            code="region_code_is_filter",
            text="regionCode は視聴可能地域の絞り込みで、その地域の視聴者への人気の断定ではない。",
        ),
        Limitation(
            code="relevance_language_is_hint",
            text="relevanceLanguage は関連度の重みづけで、結果の言語の保証ではない。",
        ),
        Limitation(
            code="video_duration_not_shorts",
            text=(
                "動画の長さ（videoDuration / duration）から Shorts と判定していない。"
                "形式は依頼の値で、確からしさは low。"
            ),
        ),
        Limitation(
            code="audience_not_measured",
            text=(
                "視聴者の地域・年齢は観測していない。"
                "audience_hypothesis は仮説であって測定値ではない。"
            ),
        ),
        Limitation(
            code="no_page_bodies",
            text="Web ページの本文は取得していない。テーマの適合は見出しと snippet だけで見た。",
        ),
    ]
    if youtube < MIN_SAMPLE_CANDIDATES:
        fixed.append(
            Limitation(
                code="small_sample",
                text=(
                    f"YouTube の候補が {youtube} 件（{MIN_SAMPLE_CANDIDATES} 件未満）で"
                    "サンプルが少ない。"
                ),
            )
        )
    if facts.uses_lifetime_average:
        fixed.append(
            Limitation(
                code="growth_is_lifetime_average",
                text=(
                    "1 時点しか観測していない動画の増加速度は公開からの平均（参考値）であり、"
                    "期間内の増加の差分ではない。"
                ),
            )
        )
    open_questions = [
        Limitation(code=f"open_question_{n:02d}", text=clip(text, 300))
        for n, text in enumerate(unknowns, start=1)
    ]
    return tuple((fixed + open_questions)[:TREND_MAX_LIMITATIONS])


# ------------------------------------------------------------------ Handler


class TrendHandler:
    """``ResearchHandler`` / ``InterpretingHandler``（Trend）。判断はここ、骨格は実行器。"""

    @property
    def kind(self) -> ResearchKind:
        return ResearchKind.TREND

    @property
    def artifact_type(self) -> ResearchArtifactType:
        return ResearchArtifactType.RESEARCH_TREND

    def plan_searches(self, spec: ResearchSpec, *, max_searches: int) -> tuple[SearchStep, ...]:
        return plan_trend_searches(_request(spec), max_searches=max_searches)

    def select_fetches(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        *,
        remaining: int,
        already_fetched: Collection[str],
    ) -> tuple[FetchTarget, ...]:
        """Trend は本文を取得しない（統計は検索結果にある。取得の枠・費用を使わない）。"""
        _request(spec)
        del rounds, remaining, already_fetched
        return ()

    def plan_interpretation(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> InterpretationTask | None:
        del fetched
        request = _request(spec)
        facts = build_trend_facts(request, rounds)
        if not facts.observations:
            return None
        return InterpretationTask(
            task_id=INTERPRETATION_TASK_ID,
            observations=tuple(
                ObservationFact(
                    observation_id=o.observation_id,
                    candidate_id=o.candidate_id,
                    metric=o.metric,
                    value=o.value,
                    unit=o.unit,
                    observed_at=o.observed_at,
                    method=o.method,
                )
                for o in facts.observations
            ),
            candidates=tuple(
                CandidateFact(
                    candidate_id=c.candidate_id,
                    theme=c.theme,
                    provider=c.provider,
                    published_at=c.references[0].published_at,
                )
                for c in facts.candidates
            ),
            context=InterpretContext(
                as_of=request.as_of,
                region=request.inputs.region,
                language=request.language,
                format_profile=request.format_profile,
                audience_hypothesis=request.inputs.audience_hypothesis,
                seed_terms=tuple(request.inputs.seed_terms),
            ),
        )

    def synthesize(
        self,
        spec: ResearchSpec,
        ctx: SynthesisContext,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> HandlerOutput:
        request = _request(spec)
        facts = build_trend_facts(request, rounds)
        planned = self.plan_interpretation(spec, rounds, fetched) is not None

        interpretations: tuple[TrendInterpretation, ...] = ()
        angles: tuple[SuggestedAngle, ...] = ()
        unknowns: tuple[str, ...] = ()
        missing: str | None = None
        warnings: list[str] = []
        if planned:
            outcome = next(
                (o for o in ctx.interpretations if o.task_id == INTERPRETATION_TASK_ID), None
            )
            if outcome is not None and outcome.proposal is not None:
                try:
                    interpretations, angles, unknowns = adopt_interpretation(
                        outcome.proposal,
                        facts.observations,
                        {c.candidate_id for c in facts.candidates},
                    )
                except InterpretationRejectedError as exc:
                    missing = f"interpreter proposal rejected: {exc}"
                    warnings.append(clip(missing, 300))
            else:
                why = (
                    outcome.error
                    if outcome is not None and outcome.error
                    else (
                        "skipped"
                        if INTERPRETATION_TASK_ID in ctx.interpretations_skipped
                        else "not run"
                    )
                )
                missing = f"interpretation not available ({why}); observations only"

        coverage, not_retrieved, gap = _coverage(rounds, ctx, facts)
        artifact = build_trend_artifact(
            request_id=ctx.request_id,
            policy_version=RESEARCH_POLICY_VERSION,
            as_of=request.as_of,
            observed_at=facts.observed_at,
            window=request.time_window,
            region=request.inputs.region,
            audience_hypothesis={"text": request.inputs.audience_hypothesis},
            language=request.language,
            format_profile=request.format_profile,
            analytics_ref=request.inputs.analytics_ref,
            queries=[_trend_query(r) for r in rounds if r.results is not None][:TREND_MAX_QUERIES],
            candidates=facts.candidates,
            observations=facts.observations,
            interpretations=interpretations,
            suggested_angles=angles,
            limitations=_limitations(facts, unknowns),
            coverage=coverage,
        )
        not_covered = [clip(f"{n.target}: {n.reason}", 300) for n in not_retrieved if gap]
        if missing is not None:
            not_covered.append(clip(f"interpretation: {missing}", 300))
        warnings.extend(
            clip(f"{r.step.step_id}: {w}", 300)
            for r in rounds
            if r.results is not None
            for w in r.results.warnings
        )
        return HandlerOutput(
            artifact_type=ResearchArtifactType.RESEARCH_TREND,
            schema_version=TREND_ARTIFACT_SCHEMA_VERSION,
            artifact=artifact,
            complete=not gap and missing is None,
            coverage=ResearchCoverage(
                items_requested=coverage.queries_planned,
                items_covered=coverage.queries_completed,
                not_covered=tuple(not_covered[:100]),
            ),
            warnings=tuple(warnings[:20]),
        )
