"""Research tool の executor（ADR-0047 §4、INV-58/59/60）。

SQLite・インメモリ store・Fake Provider のみ。実 API・実ネットワークには出ない。
理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import json

import pytest

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agent_tools_research import (
    FetchSourcesInput,
    SearchHistoricalFactsOutput,
    SearchTrendsOutput,
)
from contracts.research import ResearchStatus
from domain.research.status import RESEARCH_TERMINAL_STATUSES
from infrastructure.agents.tools.research import build
from infrastructure.db.research_repositories import ResearchRequestRepository
from tests.support.agent_tools_research import (
    FACTS_INPUT,
    INJECTION,
    NONE,
    REAL,
    TRENDS_INPUT,
    ctx,
    injected_corpus,
    make_world,
    providers,
)
from tests.support.research_evidence import stored_evidence

TRENDS = ToolId.SEARCH_TRENDS
FACTS = ToolId.SEARCH_HISTORICAL_FACTS
SOURCES = ToolId.FETCH_SOURCES


async def _request(world, request_id: str):
    async with world.session_factory() as session:
        return await ResearchRequestRepository(session).get(request_id)


# ------------------------------------------------------------------ search_trends


async def test_search_trends_returns_a_summary_with_hypotheses_flagged(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    out = await world.tools[TRENDS](ctx(), TRENDS_INPUT)

    assert out.status is ToolStatus.OK
    assert isinstance(out.data, SearchTrendsOutput)
    assert out.data.candidates
    assert out.data.region == "JP" and out.data.region_meaning == "viewable_region_filter"
    assert out.data.audience_hypothesis.kind == "hypothesis"
    assert out.data.audience_hypothesis.measured is False
    assert all(i.kind == "hypothesis" for i in out.data.interpretations)
    assert "audience_is_hypothesis" in out.limitations
    assert "provider=fake" in out.limitations
    rid = out.research_request_id
    assert rid is not None
    assert f"research_request:{rid}" in out.evidence_refs
    assert f"research_artifact:{out.data.artifact_id}" in out.evidence_refs

    request = await _request(world, rid)
    assert request is not None
    assert request.idempotency_key == ctx().idempotency_key
    assert request.payload["requester"] == "agent:strategy@1.1"
    assert request.payload["inputs"]["audience_hypothesis"] == "20代の歴史好き"


async def test_search_trends_output_has_no_page_text_or_excerpt(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    out = await world.tools[TRENDS](ctx(), TRENDS_INPUT)
    assert out.data is not None
    assert "excerpt" not in json.dumps(out.data.model_dump(mode="json"))


# ------------------------------------------------------------------ search_historical_facts


async def test_historical_facts_pass_the_assessment_through_unchanged(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    out = await world.tools[FACTS](ctx(), FACTS_INPUT)

    assert out.status is ToolStatus.OK
    assert isinstance(out.data, SearchHistoricalFactsOutput)
    assert out.research_request_id is not None
    _, artifact = await stored_evidence(session_factory, artifact_store, out.research_request_id)
    got = {c.claim_id: c for c in out.data.claims}
    assert set(got) == {c.claim_id for c in artifact.claims}
    for claim in artifact.claims:
        view = got[claim.claim_id]
        assert view.assessment == claim.assessment.value  # 変えない
        assert view.assessed is claim.assessed
        linked = {link.source_id for link in artifact.links if link.claim_id == claim.claim_id}
        assert set(view.source_ids) == linked
    assert {c.assessment for c in out.data.claims} <= {
        "supported",
        "qualified",
        "disputed",
        "insufficient",
    }
    dumped = json.dumps(out.data.model_dump(mode="json"))
    assert "confidence" not in dumped and "score" not in dumped
    assert "provider=fake" in out.limitations


async def test_page_text_and_injected_instructions_never_reach_the_tool_output(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store, prov=providers(injected_corpus()))
    out = await world.tools[FACTS](ctx(), FACTS_INPUT)
    assert out.status is ToolStatus.OK and out.data is not None
    assert out.research_request_id is not None

    # 前提: 注入文は取得した本文から抜粋として成果物に入っている（入っていなければ検査が空）
    _, artifact = await stored_evidence(session_factory, artifact_store, out.research_request_id)
    assert any(INJECTION in link.excerpt for link in artifact.links)

    sources = await world.tools[SOURCES](
        ctx(2), FetchSourcesInput(research_request_id=out.research_request_id)
    )
    for outcome in (out, sources):
        assert INJECTION not in json.dumps(outcome.data.model_dump(mode="json"))  # type: ignore[union-attr]
        assert "evil.example" not in repr(outcome)


# ------------------------------------------------------------------ fetch_sources


async def test_fetch_sources_is_a_reference_view_over_the_evidence_sources(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    facts = await world.tools[FACTS](ctx(), FACTS_INPUT)
    assert facts.research_request_id is not None
    _, artifact = await stored_evidence(session_factory, artifact_store, facts.research_request_id)

    out = await world.tools[SOURCES](
        ctx(2), FetchSourcesInput(research_request_id=facts.research_request_id)
    )
    assert out.status is ToolStatus.OK and out.data is not None
    assert {s.url for s in out.data.sources} == {s.url for s in artifact.sources}  # type: ignore[union-attr]
    first = out.data.sources[0]  # type: ignore[union-attr]
    original = next(s for s in artifact.sources if s.source_id == first.source_id)
    assert first.content_sha256 == original.content_sha256
    assert first.fetch_status == original.fetch_status.value
    assert first.source_kind == original.source_kind.value
    assert "excerpt" not in json.dumps(out.data.model_dump(mode="json"))
    assert out.research_request_id == facts.research_request_id
    # 参照だけ: 新しい依頼も外部呼び出しも作らない
    assert await world.count() == 1


async def test_fetch_sources_refuses_arbitrary_urls_without_touching_anything(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    out = await world.tools[SOURCES](ctx(), FetchSourcesInput(url="https://a.example/page"))
    assert out.status is ToolStatus.NOT_SUPPORTED
    assert out.data is None
    assert await world.count() == 0 and await world.calls() == 0
    assert world.starter.started == []


async def test_fetch_sources_for_unknown_or_trend_requests(session_factory, artifact_store) -> None:
    world = make_world(session_factory, artifact_store)
    missing = await world.tools[SOURCES](
        ctx(), FetchSourcesInput(research_request_id="22222222-2222-4222-8222-222222222222")
    )
    assert missing.status is ToolStatus.NO_DATA
    trend = await world.tools[TRENDS](ctx(2), TRENDS_INPUT)
    wrong = await world.tools[SOURCES](
        ctx(3), FetchSourcesInput(research_request_id=trend.research_request_id)
    )
    assert wrong.status is ToolStatus.NOT_SUPPORTED


# ------------------------------------------------------------------ 冪等・pending


async def test_activity_retry_with_the_same_context_creates_one_request_and_no_new_calls(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store)
    first = await world.tools[FACTS](ctx(), FACTS_INPUT)
    calls_after_first = await world.calls()
    assert calls_after_first > 0

    again = await world.tools[FACTS](ctx(), FACTS_INPUT)  # Activity の retry
    assert again.status is ToolStatus.OK
    assert again.research_request_id == first.research_request_id
    assert await world.count() == 1
    assert await world.calls() == calls_after_first  # 二重の外部呼び出し・課金なし


async def test_trend_retry_is_idempotent_too(session_factory, artifact_store) -> None:
    world = make_world(session_factory, artifact_store)
    first = await world.tools[TRENDS](ctx(), TRENDS_INPUT)
    calls = await world.calls()
    again = await world.tools[TRENDS](ctx(), TRENDS_INPUT)
    assert again.research_request_id == first.research_request_id
    assert await world.count() == 1 and await world.calls() == calls


async def test_a_running_request_returns_pending_and_the_same_key_resumes_it(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store, run=False)
    pending = await world.tools[FACTS](ctx(), FACTS_INPUT)

    assert pending.status is ToolStatus.PENDING and pending.data is None
    assert pending.research_request_id is not None
    assert f"research_request:{pending.research_request_id}" in pending.evidence_refs
    request = await _request(world, pending.research_request_id)
    assert request is not None and request.status not in RESEARCH_TERMINAL_STATUSES
    # 待ちは有界（3 秒 / 1 秒）。待つたびに heartbeat を送る
    assert len(world.sleeps) == 3 and len(world.heartbeats) == 3

    world.starter.run = True  # Worker が動き出した
    done = await world.tools[FACTS](ctx(), FACTS_INPUT)  # 同じ ID で戻ってくる
    assert done.status is ToolStatus.OK
    assert done.research_request_id == pending.research_request_id
    assert await world.count() == 1
    calls = await world.calls()
    await world.tools[FACTS](ctx(), FACTS_INPUT)
    assert await world.calls() == calls


# ------------------------------------------------------------------ 状態の写像


async def test_provider_none_maps_to_provider_not_configured_without_any_call(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store, config=NONE)
    out = await world.tools[TRENDS](ctx(), TRENDS_INPUT)
    assert out.status is ToolStatus.PROVIDER_NOT_CONFIGURED
    assert out.error_code == "provider_not_configured"
    assert out.data is None
    assert "provider=none" in out.limitations
    assert out.research_request_id is not None
    again = await world.tools[FACTS](ctx(2), FACTS_INPUT)
    assert again.status is ToolStatus.PROVIDER_NOT_CONFIGURED
    assert await world.calls() == 0


async def test_budget_not_set_fails_closed_with_its_code(session_factory, artifact_store) -> None:
    world = make_world(session_factory, artifact_store, config=REAL, prov=providers())
    out = await world.tools[FACTS](ctx(), FACTS_INPUT)
    assert out.status is ToolStatus.FAILED
    assert out.error_code == "budget_not_set"
    assert await world.calls() == 0


async def test_ambiguous_call_is_reported_and_not_retried(session_factory, artifact_store) -> None:
    from contracts.research import ResearchStopCode
    from domain.research.admission import StopReason
    from infrastructure.research.gateway import block_request

    world = make_world(session_factory, artifact_store, run=False)

    class Blocker:
        started: list[str] = []

        async def start_research(self, *, request_id: str) -> str:
            self.started.append(request_id)
            async with session_factory() as session:
                req = await ResearchRequestRepository(session).get(request_id)
                assert req is not None
                await block_request(
                    session, req, StopReason(ResearchStopCode.AMBIGUOUS_CALL, "unreconciled")
                )
                await session.commit()
            return "wf"

    tools = build(
        gateway=world.gateway,
        starter=Blocker(),
        session_factory=session_factory,
        store=artifact_store,
        channel_id="channel-1",
        provider_mode="fake",
        poll_seconds=1.0,
        wait_seconds=3.0,
        sleep=lambda s: _noop(),
    )
    out = await tools[FACTS](ctx(), FACTS_INPUT)
    assert out.status is ToolStatus.FAILED and out.error_code == "ambiguous_call"
    assert await world.calls() == 0


async def _noop() -> None:
    return None


@pytest.mark.parametrize(
    ("error_type", "research_status"),
    [
        ("ResearchSourceUnavailableError", ResearchStatus.FAILED),
        ("SomethingUnknown", ResearchStatus.BLOCKED),  # 未知の型は人の確認待ち（blocked）
    ],
)
async def test_failed_or_stuck_research_maps_to_failed_with_its_code(
    session_factory, artifact_store, error_type, research_status
) -> None:
    world = make_world(session_factory, artifact_store, fail_with=error_type)
    out = await world.tools[FACTS](ctx(), FACTS_INPUT)
    request = await _request(world, out.research_request_id or "")
    assert request is not None and request.status is research_status
    assert out.status is ToolStatus.FAILED and out.error_code == "execution_failed"
    assert out.data is None


async def test_partial_trend_is_ok_only_when_usable(session_factory, artifact_store) -> None:
    world = make_world(session_factory, artifact_store, prov=providers(interpreter=False))
    out = await world.tools[TRENDS](ctx(), TRENDS_INPUT)
    request = await _request(world, out.research_request_id or "")
    assert request is not None and request.status is ResearchStatus.PARTIAL
    # 観測だけの partial: 候補があるので使える。ただし partial と解釈なしを明示する
    assert out.status is ToolStatus.OK and out.data is not None
    assert out.data.interpretations == ()  # type: ignore[union-attr]
    assert "research_partial" in out.limitations


async def test_partial_evidence_without_any_assessment_is_insufficient_data(
    session_factory, artifact_store
) -> None:
    world = make_world(session_factory, artifact_store, prov=providers(assessor=False))
    out = await world.tools[FACTS](ctx(), FACTS_INPUT)
    request = await _request(world, out.research_request_id or "")
    assert request is not None and request.status is ResearchStatus.PARTIAL
    assert out.status is ToolStatus.INSUFFICIENT_DATA and out.data is None
    assert "research_partial" in out.limitations
    assert out.research_request_id is not None
