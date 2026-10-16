"""TrendHandler の純粋な判断（ADR-0039 §1 / §3）。I/O なし。

- 検索計画は決定的で、``max_searches`` と依頼の期間を越えない。長さの条件（videoDuration）を使わない
- Trend は本文を取得しない
- 観測が無ければ解釈器を呼ばない（解釈を計画しない）
- 解釈の提案は検査して採用する（存在しない観測 ID 等は提案ごと捨てる）

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.research import TrendResearchRequest, parse_research_spec
from contracts.research_trend import (
    InterpretationProposal,
    ProposedAngle,
    ProposedInterpretation,
)
from domain.research.handlers import InterpretingHandler, SearchRound
from domain.research.ports import SearchResults
from domain.research.trend_handler import (
    InterpretationRejectedError,
    TrendHandler,
    adopt_interpretation,
    build_trend_facts,
)
from domain.research.trend_planning import plan_trend_searches
from infrastructure.research.fake_providers import FIXED_NOW, FakeSearchProvider
from tests.support.research_trend import TREND_AS_OF, trend_request_payload


def _spec(**overrides) -> TrendResearchRequest:
    spec = parse_research_spec(trend_request_payload(**overrides))
    assert isinstance(spec, TrendResearchRequest)
    return spec


async def _rounds(spec) -> list[SearchRound]:
    search = FakeSearchProvider()
    return [
        SearchRound(step=step, results=await search.search(step.query))
        for step in plan_trend_searches(spec, max_searches=5)
    ]


def test_the_plan_is_deterministic_bounded_and_inside_the_window() -> None:
    spec = _spec()
    plan = plan_trend_searches(spec, max_searches=5)
    assert plan == plan_trend_searches(spec, max_searches=5)
    assert len(plan) == 5 and len(plan_trend_searches(spec, max_searches=2)) == 2
    assert plan_trend_searches(spec, max_searches=0) == ()
    assert {s.query.kind for s in plan} == {"youtube", "web"}
    for step in plan:
        assert step.query.published_after is not None
        assert spec.time_window.start <= step.query.published_after < TREND_AS_OF
        assert step.query.published_before == TREND_AS_OF
        assert step.query.region_code == "JP" and step.query.language == "ja"
    windows = {s.query.published_after for s in plan if s.query.kind == "youtube"}
    assert windows == {TREND_AS_OF - timedelta(days=7), TREND_AS_OF - timedelta(days=30)}


def test_shorts_is_only_a_search_hint() -> None:
    shorts = plan_trend_searches(_spec(), max_searches=3)
    long = plan_trend_searches(_spec(format_profile="long"), max_searches=3)
    assert all(s.query.text.endswith(" shorts") for s in shorts if s.query.kind == "youtube")
    assert all("shorts" not in s.query.text for s in long)


async def test_trend_fetches_no_bodies_and_is_an_interpreting_handler() -> None:
    spec = _spec()
    handler = TrendHandler()
    assert isinstance(handler, InterpretingHandler)
    rounds = await _rounds(spec)
    assert handler.select_fetches(spec, rounds, remaining=10, already_fetched=()) == ()


async def test_no_observation_means_no_interpretation_call() -> None:
    spec = _spec()
    empty = [
        SearchRound(
            step=step,
            results=SearchResults(
                query=step.query,
                hits=(),
                searched_at=FIXED_NOW,
                provider="fake",
                cost_units=0,
                truncated=False,
            ),
        )
        for step in plan_trend_searches(spec, max_searches=2)
    ]
    assert TrendHandler().plan_interpretation(spec, empty, ()) is None


async def test_the_handler_rejects_an_evidence_request() -> None:
    from tests.support.research import evidence_payload

    evidence = parse_research_spec(evidence_payload())
    with pytest.raises(Exception, match="trend research request"):
        TrendHandler().plan_searches(evidence, max_searches=3)


async def test_adoption_rejects_the_whole_proposal_on_any_violation() -> None:
    spec = _spec()
    facts = build_trend_facts(spec, await _rounds(spec))
    first = facts.observations[0]
    candidates = {c.candidate_id for c in facts.candidates}

    def proposal(*interpretations, angles=()) -> InterpretationProposal:
        return InterpretationProposal(interpretations=interpretations, angles=angles, unknowns=())

    good = ProposedInterpretation(
        key="h1",
        candidate_id=first.candidate_id,
        text="関心がある可能性がある（仮説）",
        basis_observation_ids=(first.observation_id,),
    )
    adopted, angles, _ = adopt_interpretation(
        proposal(
            good,
            angles=(
                ProposedAngle(candidate_id=None, text="切り口", basis_interpretation_keys=("h1",)),
            ),
        ),
        facts.observations,
        candidates,
    )
    assert adopted[0].interpretation_id == "I-001" and angles[0].basis_interpretation_ids == (
        "I-001",
    )

    bad_cases = [
        proposal(good.model_copy(update={"basis_observation_ids": ("O-999",)})),
        proposal(good.model_copy(update={"text": "総合スコア 87"})),
        proposal(good.model_copy(update={"text": "ランキング 1 位の題材"})),
        proposal(good, good),  # 局所名の重複
        proposal(good.model_copy(update={"candidate_id": "T-099"})),
        proposal(
            good,
            angles=(ProposedAngle(candidate_id=None, text="x", basis_interpretation_keys=("zz",)),),
        ),
    ]
    for bad in bad_cases:
        with pytest.raises(InterpretationRejectedError):
            adopt_interpretation(bad, facts.observations, candidates)
