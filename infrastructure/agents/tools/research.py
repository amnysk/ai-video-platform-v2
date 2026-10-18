"""Research tool の executor（ADR-0047 §4）。既存の Research を呼ぶ adapter（Searcher は作らない）。

手順（3 tool 共通）: ``ResearchGateway.submit``（冪等キー = ``ctx.idempotency_key``）→
``queued`` なら
``ResearchWorkflowStarter`` で起動 → ``wait_seconds`` まで DB を有界にポーリング（heartbeat）→
終端なら成果物を読み戻して sha256 を照合し、**要約**だけを返す。

- 台帳（``research_calls``）と予算の門は Research の Gateway / 実行器が持つ。ここは外部を呼ばない。
  Activity の retry は同じ ``idempotency_key`` で同じ依頼に戻る（二重の依頼・課金なし。INV-59）
- 待ちきれなければ ``pending``（依頼は走り続ける。同じ ID で呼び直せば同じ依頼に戻る）
- 出力は ID・評価・メタデータ・出典の参照だけ。外部ページの抜粋・本文（``excerpt`` /
  ``usable_expression``）は返さない（INV-60）。数値の信頼度は作らない
- 任意 URL の取得は実装しない（``not_supported``）。``HttpContentFetcher`` を台帳の外で呼ばない
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agent_tools_research import (
    MAX_SOURCES_RETURNED,
    SEARCH_HISTORICAL_FACTS_DEF,
    SEARCH_TRENDS_DEF,
    AudienceHypothesisView,
    ClaimAssessmentView,
    ClaimLink,
    CoverageView,
    FetchSourcesInput,
    FetchSourcesOutput,
    GapView,
    HypothesisView,
    LimitationView,
    MetricView,
    SearchHistoricalFactsInput,
    SearchHistoricalFactsOutput,
    SearchTrendsInput,
    SearchTrendsOutput,
    SourceRef,
    SourceView,
    TrendCandidateView,
)
from contracts.research import (
    EvidenceResearchInputs,
    EvidenceResearchSubmit,
    ResearchArtifactType,
    ResearchKind,
    ResearchResult,
    ResearchStatus,
    ResearchStopCode,
    TimeWindow,
    TrendResearchInputs,
    TrendResearchSubmit,
)
from contracts.research_evidence import EvidenceArtifact, parse_evidence_artifact
from contracts.research_trend import MetricReading, TrendArtifact, parse_trend_artifact
from domain.agents.tools.ports import ToolContext, ToolExecutor, ToolOutcome
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.research.entities import ResearchRequest
from domain.research.errors import ResearchIdempotencyConflictError
from domain.research.keys import research_artifact_object_key
from infrastructure.db.research_repositories import ResearchArtifactRepository
from infrastructure.research.gateway import ResearchGateway
from infrastructure.storage.artifact_store import ArtifactStore, readback_sha256
from infrastructure.temporal.research_starter import ResearchWorkflowStarter

__all__ = ["ResearchTools", "build"]

#: 歴史の主張は古い資料にも拠る。資料の公開日で絞らない（台本の照合と同じ窓）。固定なので
#: Activity の retry で ``request_hash`` が変わらない
_EVIDENCE_WINDOW = TimeWindow(
    start=datetime(1900, 1, 1, tzinfo=UTC), end=datetime(2100, 1, 1, tzinfo=UTC)
)
_EVIDENCE_AUDIENCE = "history viewers"
_MAX_TEXT = 300
#: tool の timeout より手前で pending を返す余裕
_TIMEOUT_MARGIN_SECONDS = 5.0


class _Unverified(Exception):  # noqa: N818 - 内部の分岐用
    """成果物を検証できなかった（理由は error_code にだけ出す）。"""


def _cut(text: str, limit: int = _MAX_TEXT) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _valid_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(UTC).date(), time(0, 0), tzinfo=UTC)


class ResearchTools:
    """外部依存（Gateway・起動・DB・store・待ち）をすべて注入する（INV-18）。"""

    def __init__(
        self,
        *,
        gateway: ResearchGateway,
        starter: ResearchWorkflowStarter,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        channel_id: str,
        provider_mode: str,
        poll_seconds: float = 2.0,
        wait_seconds: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        heartbeat: Callable[[], None] | None = None,
    ) -> None:
        self._gateway = gateway
        self._starter = starter
        self._session_factory = session_factory
        self._store = store
        self._channel_id = channel_id
        self._provider_mode = provider_mode
        self._poll_seconds = max(poll_seconds, 0.001)
        self._wait_override = wait_seconds
        self._sleep = sleep
        self._heartbeat = heartbeat or (lambda: None)

    def executors(self) -> dict[ToolId, ToolExecutor]:
        return {
            ToolId.SEARCH_TRENDS: self.search_trends,
            ToolId.SEARCH_HISTORICAL_FACTS: self.search_historical_facts,
            ToolId.FETCH_SOURCES: self.fetch_sources,
        }

    # ------------------------------------------------------------------ search_trends

    async def search_trends(self, ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome:
        inp = SearchTrendsInput.model_validate(tool_input.model_dump())
        # 同じ日の retry で ``time_window`` が変わらないよう日の境界に丸める（hash を安定させる）
        end = _day_start(ctx.now) + timedelta(days=1)
        submit = TrendResearchSubmit(
            kind=ResearchKind.TREND,
            idempotency_key=ctx.idempotency_key,
            requester=f"agent:{ctx.agent_ref}"[:64],
            channel_id=self._channel_id,
            episode_id=_valid_uuid(ctx.episode_id),
            audience=inp.audience_hypothesis[:200],
            language=inp.language,
            format_profile=inp.format_profile,
            time_window=TimeWindow(start=end - timedelta(days=inp.window_days), end=end),
            as_of=ctx.now,
            inputs=TrendResearchInputs(
                region=inp.region,
                audience_hypothesis=inp.audience_hypothesis,
                seed_terms=inp.seed_terms,
            ),
        )
        return await self._run(
            submit,
            timeout=SEARCH_TRENDS_DEF.timeout_seconds,
            artifact_type=ResearchArtifactType.RESEARCH_TREND,
            summarize=self._summarize_trend,
            base_limitations=("audience_is_hypothesis",),
        )

    def _summarize_trend(
        self, artifact: BaseModel, request: ResearchRequest, partial: bool
    ) -> _Summary:
        assert isinstance(artifact, TrendArtifact)
        candidates = tuple(
            TrendCandidateView(
                candidate_id=c.candidate_id,
                theme=_cut(c.theme, 200),
                sources=tuple(
                    SourceRef(
                        url=r.url,
                        title=_cut(r.title),
                        published_at=r.published_at.isoformat() if r.published_at else None,
                    )
                    for r in c.references
                ),
                growth_observation=_metric(c.metrics.growth_observation),
                age_since_publish=_metric(c.metrics.age_since_publish),
                data_gaps=tuple(_cut(g) for g in c.metrics.data_gaps),
            )
            for c in artifact.candidates
        )
        data = SearchTrendsOutput(
            research_request_id=request.id,
            artifact_id="",  # _run が成果物の ID で置き換える
            observed_at=artifact.observed_at.isoformat(),
            region=artifact.region,
            audience_hypothesis=AudienceHypothesisView(text=artifact.audience_hypothesis.text),
            candidates=candidates,
            interpretations=tuple(
                HypothesisView(
                    id=i.interpretation_id, text=_cut(i.text), candidate_id=i.candidate_id
                )
                for i in artifact.interpretations
            ),
            suggested_angles=tuple(
                HypothesisView(id=a.angle_id, text=_cut(a.text), candidate_id=a.candidate_id)
                for a in artifact.suggested_angles
            ),
            coverage=CoverageView(
                items_requested=artifact.coverage.queries_planned,
                items_covered=artifact.coverage.queries_completed,
                not_covered=tuple(
                    _cut(f"{n.target}: {n.reason}") for n in artifact.coverage.not_retrieved
                ),
            ),
            limitations=tuple(
                LimitationView(code=lim.code, text=_cut(lim.text)) for lim in artifact.limitations
            ),
        )
        usable = bool(candidates)
        return _Summary(
            data=data,
            usable=usable,
            empty=not candidates,
            retrieved_at=artifact.observed_at.isoformat(),
            limitations=tuple(f"trend:{lim.code}" for lim in artifact.limitations),
        )

    # ------------------------------------------------------------------ search_historical_facts

    async def search_historical_facts(self, ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome:
        inp = SearchHistoricalFactsInput.model_validate(tool_input.model_dump())
        submit = EvidenceResearchSubmit(
            kind=ResearchKind.EVIDENCE,
            idempotency_key=ctx.idempotency_key,
            requester=f"agent:{ctx.agent_ref}"[:64],
            channel_id=self._channel_id,
            episode_id=_valid_uuid(ctx.episode_id),
            audience=_EVIDENCE_AUDIENCE,
            language=inp.language,
            format_profile="any",
            time_window=_EVIDENCE_WINDOW,
            as_of=ctx.now,
            inputs=EvidenceResearchInputs(claim_inputs=inp.claims),
        )
        return await self._run(
            submit,
            timeout=SEARCH_HISTORICAL_FACTS_DEF.timeout_seconds,
            artifact_type=ResearchArtifactType.RESEARCH_EVIDENCE,
            summarize=self._summarize_evidence,
            base_limitations=(),
        )

    def _summarize_evidence(
        self, artifact: BaseModel, request: ResearchRequest, partial: bool
    ) -> _Summary:
        assert isinstance(artifact, EvidenceArtifact)
        claims = []
        for claim in artifact.claims:
            links = [link for link in artifact.links if link.claim_id == claim.claim_id]
            claims.append(
                ClaimAssessmentView(
                    claim_id=claim.claim_id,
                    claim_text=_cut(claim.text),
                    kind=claim.kind.value,
                    importance=claim.importance.value,
                    strong=claim.strong,
                    assessed=claim.assessed,
                    assessment=claim.assessment.value,  # Research の評価をそのまま
                    assessment_reason=_cut(claim.assessment_reason),
                    source_ids=tuple(dict.fromkeys(link.source_id for link in links)),
                    links=tuple(
                        ClaimLink(source_id=link.source_id, stance=link.stance.value)
                        for link in links
                    ),
                    unverified_points=tuple(_cut(p) for p in claim.unverified_points),
                )
            )
        stop = _stop_code(request)
        data = SearchHistoricalFactsOutput(
            research_request_id=request.id,
            artifact_id="",
            observed_at=artifact.as_of.isoformat(),
            claims=tuple(claims),
            gaps=tuple(
                GapView(
                    claim_id=g.claim_id,
                    description=_cut(g.description),
                    suggested_query=g.suggested_query,
                )
                for g in artifact.gaps
            ),
            coverage=CoverageView(
                items_requested=len(artifact.claims),
                items_covered=sum(1 for c in artifact.claims if c.assessed),
            ),
            limitations=(
                (LimitationView(code=stop, text="research stopped early"),)
                if partial and stop
                else ()
            ),
        )
        return _Summary(
            data=data,
            usable=any(c.assessed for c in artifact.claims),
            empty=False,
            retrieved_at=artifact.as_of.isoformat(),
            limitations=(),
        )

    # ------------------------------------------------------------------ fetch_sources

    async def fetch_sources(self, ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome:
        inp = FetchSourcesInput.model_validate(tool_input.model_dump())
        if inp.url is not None:
            # 任意 URL は取得しない（台帳・予算の外で HttpContentFetcher を呼ぶ経路を作らない）
            return ToolOutcome(
                status=ToolStatus.NOT_SUPPORTED,
                limitations=("arbitrary_url_fetch_not_supported",),
                error_code="arbitrary_url_not_supported",
            )
        assert inp.research_request_id is not None
        request = await self._gateway.get(inp.research_request_id)
        if request is None:
            return ToolOutcome(status=ToolStatus.NO_DATA, limitations=("unknown_research_request",))
        if request.kind is not ResearchKind.EVIDENCE:
            return ToolOutcome(
                status=ToolStatus.NOT_SUPPORTED,
                research_request_id=request.id,
                limitations=("request_has_no_sources",),
                error_code="not_an_evidence_request",
            )
        return await self._settle(
            request,
            artifact_type=ResearchArtifactType.RESEARCH_EVIDENCE,
            summarize=self._summarize_sources,
            base_limitations=(),
        )

    def _summarize_sources(
        self, artifact: BaseModel, request: ResearchRequest, partial: bool
    ) -> _Summary:
        assert isinstance(artifact, EvidenceArtifact)
        sources = tuple(
            SourceView(
                source_id=s.source_id,
                url=s.url,
                title=_cut(s.title),
                language=s.language,
                published_at=s.published_at.isoformat() if s.published_at else None,
                retrieved_at=s.retrieved_at.isoformat(),
                content_sha256=s.content_sha256,
                fetch_status=s.fetch_status.value,
                source_kind=s.source_kind.value,
            )
            for s in artifact.sources[:MAX_SOURCES_RETURNED]
        )
        truncated = len(artifact.sources) > MAX_SOURCES_RETURNED
        data = FetchSourcesOutput(
            research_request_id=request.id,
            artifact_id="",
            sources=sources,
            limitations=(
                (LimitationView(code="sources_truncated", text="more sources were recorded"),)
                if truncated
                else ()
            ),
        )
        return _Summary(
            data=data,
            usable=True,
            empty=not sources,
            retrieved_at=artifact.as_of.isoformat(),
            limitations=("sources_truncated",) if truncated else (),
        )

    # ------------------------------------------------------------------ 共通

    async def _run(
        self,
        submit: TrendResearchSubmit | EvidenceResearchSubmit,
        *,
        timeout: int,
        artifact_type: ResearchArtifactType,
        summarize: _Summarizer,
        base_limitations: tuple[str, ...],
    ) -> ToolOutcome:
        provider = (f"provider={self._provider_mode}",) if self._provider_mode in _LABELLED else ()
        try:
            submitted = await self._gateway.submit(submit)
        except ResearchIdempotencyConflictError:
            return ToolOutcome(
                status=ToolStatus.FAILED,
                limitations=provider,
                error_code="idempotency_conflict",
            )
        request = submitted.request
        if request.status is ResearchStatus.QUEUED:
            # 実行中の同じ依頼は Temporal が二重に走らせない（start は冪等）
            await self._starter.start_research(request_id=request.id)
        request = await self._wait(request, timeout)
        return await self._settle(
            request,
            artifact_type=artifact_type,
            summarize=summarize,
            base_limitations=(*base_limitations, *provider),
        )

    async def _wait(self, request: ResearchRequest, timeout: int) -> ResearchRequest:
        """終端か ``blocked`` まで DB を有界に読む。上限を越えたら最後に見た状態を返す。"""
        budget = self._wait_override
        if budget is None:
            budget = max(float(timeout) - _TIMEOUT_MARGIN_SECONDS, 0.0)
        polls = int(budget / self._poll_seconds)
        current = request
        for _ in range(polls):
            if _settled(current):
                return current
            self._heartbeat()
            await self._sleep(self._poll_seconds)
            latest = await self._gateway.get(request.id)
            if latest is None:
                raise LookupError("the research request disappeared")
            current = latest
        return current

    async def _settle(
        self,
        request: ResearchRequest,
        *,
        artifact_type: ResearchArtifactType,
        summarize: _Summarizer,
        base_limitations: tuple[str, ...],
    ) -> ToolOutcome:
        refs = (f"research_request:{request.id}",)
        label = (f"provider={self._provider_mode}",) if self._provider_mode in _LABELLED else ()
        limitations = tuple(dict.fromkeys((*base_limitations, *label)))
        status = request.status
        if status in (ResearchStatus.QUEUED, ResearchStatus.RUNNING):
            return ToolOutcome(
                status=ToolStatus.PENDING,
                limitations=limitations,
                evidence_refs=refs,
                research_request_id=request.id,
            )
        if status is ResearchStatus.BLOCKED:
            code = str((request.blocked_reason or {}).get("code") or "blocked")
            mapped = (
                ToolStatus.PROVIDER_NOT_CONFIGURED
                if code == ResearchStopCode.PROVIDER_NOT_CONFIGURED.value
                else ToolStatus.FAILED
            )
            return ToolOutcome(
                status=mapped,
                limitations=limitations,
                evidence_refs=refs,
                research_request_id=request.id,
                error_code=code,
            )
        if status is ResearchStatus.FAILED:
            return ToolOutcome(
                status=ToolStatus.FAILED,
                limitations=limitations,
                evidence_refs=refs,
                research_request_id=request.id,
                error_code=_stop_code(request) or "execution_failed",
            )
        partial = status is ResearchStatus.PARTIAL
        try:
            artifact_id, artifact = await self._load(request, artifact_type)
        except _Unverified as exc:
            return ToolOutcome(
                status=ToolStatus.FAILED,
                limitations=limitations,
                evidence_refs=refs,
                research_request_id=request.id,
                error_code=str(exc),
            )
        summary = summarize(artifact, request, partial)
        extra = ("research_partial",) if partial else ()
        stop = _stop_code(request) if partial else None
        all_limitations = tuple(
            dict.fromkeys(
                (*limitations, *extra, *((f"stop:{stop}",) if stop else ()), *summary.limitations)
            )
        )[:20]
        evidence = (*refs, f"research_artifact:{artifact_id}")
        retrieved = summary.retrieved_at
        if partial and not summary.usable:
            return ToolOutcome(
                status=ToolStatus.INSUFFICIENT_DATA,
                limitations=all_limitations,
                evidence_refs=evidence,
                research_request_id=request.id,
                retrieved_at=retrieved,
            )
        if summary.empty and not partial:
            return ToolOutcome(
                status=ToolStatus.NO_DATA,
                limitations=all_limitations,
                evidence_refs=evidence,
                research_request_id=request.id,
                retrieved_at=retrieved,
            )
        data = summary.data.model_copy(update={"artifact_id": artifact_id})
        return ToolOutcome(
            status=ToolStatus.OK,
            data=data,
            limitations=all_limitations,
            evidence_refs=evidence,
            research_request_id=request.id,
            retrieved_at=retrieved,
        )

    async def _load(
        self, request: ResearchRequest, artifact_type: ResearchArtifactType
    ) -> tuple[str, BaseModel]:
        """結果が指す現行の成果物を読み戻し、sha256 とキーを照合して契約で読む。"""
        try:
            result = ResearchResult.model_validate(request.result_summary or {})
        except ValidationError as exc:
            raise _Unverified("result_invalid") from exc
        refs = [r for r in result.artifact_refs if r.artifact_type is artifact_type]
        if len(refs) != 1:
            raise _Unverified("artifact_missing")
        ref = refs[0]
        async with self._session_factory() as session:
            records = await ResearchArtifactRepository(session).list_current(request.id)
        record = next(
            (
                a
                for a in records
                if (a.id, a.artifact_type, a.sha256)
                == (ref.artifact_id, ref.artifact_type, ref.sha256)
            ),
            None,
        )
        if record is None:
            raise _Unverified("artifact_not_current")
        if record.object_key != research_artifact_object_key(
            request.id, record.artifact_type, record.sha256
        ):
            raise _Unverified("artifact_key_mismatch")
        try:
            stored = await readback_sha256(self._store, record.object_key)
            payload = await self._store.get_json(record.object_key)
        except KeyError as exc:
            raise _Unverified("artifact_missing") from exc
        if stored != record.sha256 or sha256_hex(canonical_json_bytes(payload)) != record.sha256:
            raise _Unverified("artifact_sha256_mismatch")
        try:
            if artifact_type is ResearchArtifactType.RESEARCH_TREND:
                artifact: BaseModel = parse_trend_artifact(payload)
            else:
                artifact = parse_evidence_artifact(payload)
        except ValidationError as exc:
            raise _Unverified("artifact_contract_violation") from exc
        if getattr(artifact, "request_id", None) != request.id:
            raise _Unverified("artifact_other_request")
        return record.id, artifact


class _Summary:
    __slots__ = ("data", "usable", "empty", "retrieved_at", "limitations")

    def __init__(
        self,
        *,
        data: Any,
        usable: bool,
        empty: bool,
        retrieved_at: str,
        limitations: tuple[str, ...],
    ) -> None:
        self.data = data
        self.usable = usable
        self.empty = empty
        self.retrieved_at = retrieved_at
        self.limitations = limitations


_Summarizer = Callable[[BaseModel, ResearchRequest, bool], _Summary]
#: 実調査と区別するラベルを付ける設定値（``fake`` は偽物、``none`` は実行できない）
_LABELLED = frozenset({"fake", "none"})


def _settled(request: ResearchRequest) -> bool:
    return request.status not in (ResearchStatus.QUEUED, ResearchStatus.RUNNING)


def _stop_code(request: ResearchRequest) -> str | None:
    try:
        result = ResearchResult.model_validate(request.result_summary or {})
    except ValidationError:
        return None
    return result.stop_code.value if result.stop_code is not None else None


def _metric(reading: MetricReading) -> MetricView:
    return MetricView(
        status=reading.status.value,
        value=reading.value,
        unit=reading.unit,
        metric=reading.metric.value if reading.metric else None,
        observed_at=reading.observed_at.isoformat() if reading.observed_at else None,
        unknown_reason=_cut(reading.unknown_reason) if reading.unknown_reason else None,
    )


def build(
    *,
    gateway: ResearchGateway,
    starter: ResearchWorkflowStarter,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    channel_id: str,
    provider_mode: str,
    poll_seconds: float = 2.0,
    wait_seconds: float | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    heartbeat: Callable[[], None] | None = None,
) -> dict[ToolId, ToolExecutor]:
    """Research の 3 tool の executor。組み立ては ``tools._research_executors``。"""
    return ResearchTools(
        gateway=gateway,
        starter=starter,
        session_factory=session_factory,
        store=store,
        channel_id=channel_id,
        provider_mode=provider_mode,
        poll_seconds=poll_seconds,
        wait_seconds=wait_seconds,
        sleep=sleep,
        heartbeat=heartbeat,
    ).executors()
