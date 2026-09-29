"""実行器を検査するための汎用 Handler（テスト専用。ADR-0037 §8）。

Trend / Evidence の本物の Handler は後続の段（ADR-0038 / ADR-0039）で入る。ここでは実行器の骨格
（予約・dispatch・決着、上限、生データ、成果物の書き込みと読み戻し）だけを動かすために、
固定の検索語から検索を計画し、各検索の上位の結果を取得し、取得できた資料を並べた成果物を返す。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any

from contracts.research import (
    RESEARCH_SCHEMA_VERSION,
    ResearchArtifactType,
    ResearchCoverage,
    ResearchKind,
)
from domain.research.handlers import (
    FetchedSource,
    FetchTarget,
    HandlerOutput,
    ResearchSpec,
    SearchRound,
    SearchStep,
    SynthesisContext,
)
from domain.research.ports import SearchKind, SearchQuery


class GenericTestHandler:
    """``queries`` を順に検索し、各検索の上位 ``fetch_per_search`` 件を取得する。"""

    def __init__(
        self,
        queries: Sequence[str] = ("関ヶ原",),
        *,
        kind: ResearchKind = ResearchKind.EVIDENCE,
        search_kind: SearchKind = "web",
        fetch_per_search: int = 1,
        max_results: int = 3,
        complete: bool = True,
    ) -> None:
        self._queries = tuple(queries)
        self._kind = kind
        self._search_kind: SearchKind = search_kind
        self._fetch_per_search = fetch_per_search
        self._max_results = max_results
        self._complete = complete
        self.synthesized: list[tuple[SynthesisContext, int, int]] = []

    @property
    def kind(self) -> ResearchKind:
        return self._kind

    @property
    def artifact_type(self) -> ResearchArtifactType:
        if self._kind is ResearchKind.TREND:
            return ResearchArtifactType.RESEARCH_TREND
        return ResearchArtifactType.RESEARCH_EVIDENCE

    def plan_searches(self, spec: ResearchSpec, *, max_searches: int) -> tuple[SearchStep, ...]:
        del spec, max_searches  # 上限を超えて計画しても実行器が落とす（検査のため素直に全部返す）
        return tuple(
            SearchStep(
                step_id=f"s{index:02d}",
                query=SearchQuery(text=text, kind=self._search_kind, max_results=self._max_results),
                purpose=f"generic:{index}",
            )
            for index, text in enumerate(self._queries, start=1)
        )

    def select_fetches(
        self,
        spec: ResearchSpec,
        rounds: Sequence[SearchRound],
        *,
        remaining: int,
        already_fetched: Collection[str],
    ) -> tuple[FetchTarget, ...]:
        del spec
        chosen: list[FetchTarget] = []
        for search_round in rounds:
            if search_round.results is None:
                continue
            for hit in search_round.results.hits[: self._fetch_per_search]:
                if hit.url in already_fetched:
                    continue
                chosen.append(FetchTarget(hit=hit, step_id=search_round.step.step_id))
        return tuple(chosen[: max(remaining, 0)])

    def synthesize(
        self,
        spec: ResearchSpec,
        ctx: SynthesisContext,
        rounds: Sequence[SearchRound],
        fetched: Sequence[FetchedSource],
    ) -> HandlerOutput:
        del spec
        self.synthesized.append((ctx, len(rounds), len(fetched)))
        usable = [r for r in rounds if r.results is not None]
        artifact: dict[str, Any] = {
            "request_id": ctx.request_id,
            "as_of": ctx.as_of.isoformat(),
            "searches": [
                {
                    "step_id": r.step.step_id,
                    "hits": [h.url for h in r.results.hits] if r.results else [],
                    "error": r.error,
                }
                for r in rounds
            ],
            "sources": [
                {
                    "url": s.content.final_url,
                    "fetch_status": s.content.fetch_status,
                    "content_sha256": s.content.content_sha256,
                    "body_confirmed": s.content.body_confirmed,
                }
                for s in fetched
            ],
        }
        return HandlerOutput(
            artifact_type=self.artifact_type,
            schema_version=RESEARCH_SCHEMA_VERSION,
            artifact=artifact,
            complete=self._complete,
            coverage=ResearchCoverage(
                items_requested=max(len(rounds), 1),
                items_covered=min(len(usable), max(len(rounds), 1)),
            ),
        )


__all__ = ["GenericTestHandler"]
