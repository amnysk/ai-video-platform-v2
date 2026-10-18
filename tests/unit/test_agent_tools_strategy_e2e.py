"""strategy@1.1 / historian@1.0 を本物の handler・Activity・executor・ToolCallStore で通す。

非 Docker: SQLite + InMemory store。LLM は台本どおりの fake、Research は provider=fake、
YouTube は httpx.MockTransport。実 API には出ない。
理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.agent_tool_registry import TOOL_DEFS
from contracts.agentic import FactCheckReport, StrategyBrief
from contracts.agents import AgentHandlerRef
from contracts.intervention import AgentRole
from contracts.states import ArtifactType
from domain.agents.handlers import HANDLERS
from domain.agents.tool_gateway import ToolSupport
from infrastructure.agents.tools.analytics import build as build_analytics
from infrastructure.agents.tools.budget import AgentToolCallBudget
from infrastructure.db.agent_tool_call_repository import AgentToolCallRepository
from infrastructure.db.models import AgentToolCallRow, ResearchCallRow, ResearchRequestRow
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.intervention.store import InterventionStore
from infrastructure.research.fake_corpus import ResearchCorpus, default_corpus
from tests.support.agent_stage import make_world as make_stage_world
from tests.support.agent_stage import stage_request
from tests.support.agent_tools_handlers import ScriptedLlm
from tests.support.agent_tools_research import FACTS_INPUT, INJECTION, TRENDS_INPUT, providers
from tests.support.agent_tools_research import make_world as make_research_world
from tests.support.agent_tools_youtube import Net, channel_ok, forbidden, make_provider

STRATEGY = AgentHandlerRef.STRATEGY_AGENT_V1
HISTORIAN = AgentHandlerRef.HISTORIAN_AGENT_V1
ERA = "sengoku"


def _plan(*extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "requests": [
            {"tool_ref": "search_trends@1", "input": TRENDS_INPUT.model_dump(mode="json")},
            {"tool_ref": "get_channel_performance@1", "input": {}},
            *extra,
        ]
    }


def _answer(**over: Any) -> dict[str, Any]:
    from contracts.topic_planning import STRATEGY_PROFILES as PROFILES

    era = PROFILES["us_young_history_v1"].eras[0]
    cand = {
        "id": "c1",
        "title": "Why samurai wore silk",
        "subject": "samurai_silk",
        "angle": "reason",
        "era": era,
        "hook": "What if everything you know is wrong?",
        "rationale": "Surprising concrete detail.",
        "risks": ["niche"],
    }
    answer: dict[str, Any] = {
        "audience_hypothesis": "Curious young viewers like concrete oddities.",
        "candidates": [cand, {**cand, "id": "c2", "title": "Castle toilets explained"}],
        "recommended_candidate_id": "c1",
        "success_criteria": ["retention above baseline"],
        "citations": [],
        "uncertainties": ["trend unknown"],
        "rationale": "Strongest hook.",
    }
    answer.update(over)
    return answer


class Env:
    """1 つのテストの世界。外部呼び出しの回数を数えられる。"""

    def __init__(self, world, research, net: Net, support: ToolSupport) -> None:
        self.world, self.research, self.net, self.support = world, research, net, support

    async def count(self, session_factory, row) -> int:
        async with session_factory() as s:
            return int(await s.scalar(select(func.count()).select_from(row)) or 0)


async def _env(session_factory, artifact_store, *, analytics=channel_ok, corpus=None, **world_kw):
    world = await make_stage_world(session_factory, artifact_store, **world_kw)
    research = make_research_world(
        session_factory, artifact_store, prov=providers(corpus) if corpus else None
    )
    net = Net(analytics)
    executors = {
        **research.tools,
        **build_analytics(make_provider(net), AgentToolCallBudget(session_factory), daily_cap=20),
    }
    support = ToolSupport(
        defs=TOOL_DEFS,
        executors=executors,
        store=AgentToolCallRepository(session_factory, artifact_store),
    )
    return Env(world, research, net, support)


async def _rows(session_factory) -> list[AgentToolCallRow]:
    async with session_factory() as s:
        return list(
            (await s.execute(select(AgentToolCallRow).order_by(AgentToolCallRow.seq))).scalars()
        )


async def _brief(env: Env) -> StrategyBrief:
    async with env.world.session_factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            env.world.episode_id, ArtifactType.STRATEGY_BRIEF
        )
    assert meta is not None
    return StrategyBrief.model_validate(await env.world.store.get_json(meta.object_key))


async def _run(env: Env, llm: ScriptedLlm, *, tools: bool = True, version: str = "1.1"):
    env.world.llm = llm  # type: ignore[assignment]
    activities = env.world.activities(HANDLERS, tool_support=env.support if tools else None)
    request, _ = stage_request(AgentRole.STRATEGY, env.world.episode_id, version=version)
    return activities, request, await activities.run_agent_stage(request)


# --------------------------------------------------------------------------- 本流


async def test_strategy_1_1_runs_end_to_end_with_real_tools_and_links_the_evidence(
    session_factory, artifact_store
) -> None:
    env = await _env(session_factory, artifact_store)
    llm = ScriptedLlm(_plan(), _answer())
    _, _, result = await _run(env, llm)
    assert result.outcome == "produced" and llm.calls == 2

    rows = await _rows(session_factory)
    assert [(r.seq, r.tool_id, r.status) for r in rows] == [
        (1, "search_trends", "ok"),
        (2, "get_channel_performance", "ok"),
    ]
    assert rows[0].input["region"] == "JP" and rows[0].research_request_id
    assert any(ref.startswith("research_request:") for ref in rows[0].evidence_refs)
    assert any(ref.startswith("analytics_query:") for ref in rows[1].evidence_refs)
    assert env.net.requests  # モックの YouTube は呼ばれた（実ネットワークではない）

    brief = await _brief(env)
    for row in rows:
        assert f"tool_call:{row.tool_call_id}" in brief.citations
    assert brief.audience_basis == "assumed"  # 実測の snapshot が無い
    async with session_factory() as s:
        (decision,) = await InterventionStore(s).list_decisions(episode_id=env.world.episode_id)
    for row in rows:
        assert f"tool_call:{row.tool_call_id}" in decision.payload["evidence_refs"]


async def test_a_replay_makes_no_new_external_call_and_adds_no_rows(
    session_factory, artifact_store
) -> None:
    env = await _env(session_factory, artifact_store)
    llm = ScriptedLlm(_plan(), _answer())
    activities, request, first = await _run(env, llm)
    rows_before = [(r.tool_call_id, r.status) for r in await _rows(session_factory)]
    net_before, llm_before = len(env.net.requests), llm.calls
    research_before = (
        await env.count(session_factory, ResearchRequestRow),
        await env.count(session_factory, ResearchCallRow),
    )
    again = await activities.run_agent_stage(request)
    assert again.replayed and again.artifact_revision_id == first.artifact_revision_id
    assert len(env.net.requests) == net_before and llm.calls == llm_before
    assert [(r.tool_call_id, r.status) for r in await _rows(session_factory)] == rows_before
    assert research_before == (
        await env.count(session_factory, ResearchRequestRow),
        await env.count(session_factory, ResearchCallRow),
    )


async def test_a_crash_after_the_tools_but_before_the_save_reuses_the_stored_results(
    session_factory, artifact_store
) -> None:
    """INV-59: 保存前に落ちた retry は同じ tool_call_id を作り、外部も LLM も繰り返さない。"""
    env = await _env(session_factory, artifact_store)
    real = HANDLERS[STRATEGY]
    attempts = {"n": 0}

    async def flaky(agent_input, gateway):
        out = await real(agent_input, gateway)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("crash before the stage was saved")
        return out

    llm = ScriptedLlm(_plan(), _answer())
    env.world.llm = llm  # type: ignore[assignment]
    activities = env.world.activities({STRATEGY: flaky}, tool_support=env.support)
    request, _ = stage_request(AgentRole.STRATEGY, env.world.episode_id, version="1.1")
    with pytest.raises(RuntimeError):
        await activities.run_agent_stage(request)
    ids = [r.tool_call_id for r in await _rows(session_factory)]
    net_after_first = len(env.net.requests)
    research_calls = await env.count(session_factory, ResearchCallRow)

    result = await activities.run_agent_stage(request)
    assert result.outcome == "produced" and llm.calls == 2  # LLM は保存済みの応答を再生
    assert [r.tool_call_id for r in await _rows(session_factory)] == ids
    assert len(env.net.requests) == net_after_first
    assert await env.count(session_factory, ResearchCallRow) == research_calls


# --------------------------------------------------------------------------- flag・1.0


async def test_with_the_tools_flag_off_1_1_cannot_run_and_1_0_works_as_before(
    session_factory, artifact_store
) -> None:
    env = await _env(session_factory, artifact_store)
    llm = ScriptedLlm(_plan(), _answer())
    with pytest.raises(ApplicationError) as err:
        await _run(env, llm, tools=False)
    assert err.value.type == "ToolNotAllowedError" and llm.calls == 0
    assert await _rows(session_factory) == [] and not env.net.requests

    env10 = await _env(session_factory, artifact_store)
    llm10 = ScriptedLlm(_answer())
    # tool support が配線されていても 1.0 は tool を持たない
    _, _, result = await _run(env10, llm10, tools=True, version="1.0")
    assert result.outcome == "produced"
    assert llm10.calls == 1 and await _rows(session_factory) == []
    assert not env10.net.requests


async def test_a_tool_less_run_still_produces_a_brief(session_factory, artifact_store) -> None:
    env = await _env(session_factory, artifact_store)
    llm = ScriptedLlm({"requests": []}, _answer())
    _, _, result = await _run(env, llm)
    assert result.outcome == "produced" and llm.calls == 2
    assert await _rows(session_factory) == [] and not env.net.requests
    assert (await _brief(env)).citations == ()


# --------------------------------------------------------------------------- 注入・上限・ループ


async def test_injection_in_a_research_source_changes_neither_the_plan_nor_the_output(
    session_factory, artifact_store
) -> None:
    corpus = ResearchCorpus(
        tuple(
            dataclasses.replace(d, title=f"{d.title} {INJECTION}")
            for d in default_corpus().documents
        )
    )
    env = await _env(session_factory, artifact_store, corpus=corpus)
    llm = ScriptedLlm(_plan(), _answer())
    await _run(env, llm)
    rows = await _rows(session_factory)
    assert len(rows) == 2 and llm.calls == 2  # 計画の 2 件だけ。「fetch_sources を呼べ」は無視
    synth = llm.prompts[1]
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in synth  # 結果は DATA として渡る
    assert synth.count("</data>") == synth.count("<data name=")
    brief = await _brief(env)
    assert brief.audience_basis == "assumed" and brief.recommended_candidate_id == "c1"
    assert all(c.subject != "hacked" for c in brief.candidates)


async def test_a_plan_with_disallowed_or_excess_tools_is_truncated_and_refused(
    session_factory, artifact_store
) -> None:
    extra = [
        {"tool_ref": "search_historical_facts@1", "input": FACTS_INPUT.model_dump(mode="json")},
        {"tool_ref": "get_channel_performance@1", "input": {"start_date": "2026-09-01"}},
        {"tool_ref": "get_channel_performance@1", "input": {"start_date": "2026-09-02"}},
    ]
    env = await _env(session_factory, artifact_store)
    llm = ScriptedLlm(_plan(*extra), _answer())
    _, _, result = await _run(env, llm)
    assert result.outcome == "produced"
    rows = await _rows(session_factory)
    # get_channel_performance は上限 2 まで。許可外の historical facts は呼ばれない
    assert [r.tool_id for r in rows] == ["search_trends", "get_channel_performance"] + [
        "get_channel_performance"
    ] * (len(rows) - 2)
    assert len(rows) == 3  # 上限 2 を超える 3 件目は呼ばれない
    assert all(r.tool_id != "search_historical_facts" for r in rows)
    assert any("refused" in u for u in (await _brief(env)).uncertainties)


async def test_a_tool_result_that_says_call_again_causes_no_extra_call(
    session_factory, artifact_store
) -> None:
    from tests.support.agent_tools_handlers import INJECTION as LOOP

    corpus = ResearchCorpus(
        tuple(dataclasses.replace(d, title=f"{d.title} {LOOP}") for d in default_corpus().documents)
    )
    env = await _env(session_factory, artifact_store, corpus=corpus)
    llm = ScriptedLlm(_plan(), _answer())
    await _run(env, llm)
    assert len(await _rows(session_factory)) == 2 and llm.calls == 2


async def test_unavailable_analytics_yields_uncertainties_and_not_measured(
    session_factory, artifact_store
) -> None:
    env = await _env(session_factory, artifact_store, analytics=forbidden)
    llm = ScriptedLlm(_plan(), _answer())
    _, _, result = await _run(env, llm)
    assert result.outcome == "produced"
    rows = await _rows(session_factory)
    assert [r.status for r in rows] == ["ok", "permission_denied"]
    brief = await _brief(env)
    assert brief.audience_basis == "assumed"
    assert any(
        "get_channel_performance" in u and "permission_denied" in u for u in brief.uncertainties
    )
    assert f"tool_call:{rows[1].tool_call_id}" in brief.citations


# --------------------------------------------------------------------------- historian


async def test_the_historian_stage_stores_a_fact_check_report_with_linked_evidence(
    session_factory, artifact_store
) -> None:
    env = await _env(session_factory, artifact_store, storyboard=True)
    plan = {
        "requests": [
            {"tool_ref": "search_historical_facts@1", "input": FACTS_INPUT.model_dump(mode="json")}
        ]
    }
    llm = ScriptedLlm(plan)
    env.world.llm = llm  # type: ignore[assignment]
    activities = env.world.activities(HANDLERS, tool_support=env.support)
    request, _ = stage_request(AgentRole.HISTORIAN, env.world.episode_id)
    result = await activities.run_agent_stage(request)
    assert result.outcome == "produced" and llm.calls == 1
    async with session_factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            env.world.episode_id, ArtifactType.FACT_CHECK_REPORT
        )
        (decision,) = await InterventionStore(s).list_decisions(episode_id=env.world.episode_id)
    assert meta is not None
    report = FactCheckReport.model_validate(await env.world.store.get_json(meta.object_key))
    (row,) = await _rows(session_factory)
    assert row.agent_role == "historian" and row.tool_id == "search_historical_facts"
    assert report.claims and all(
        f"tool_call:{row.tool_call_id}" in c.evidence_refs for c in report.claims
    )
    assert decision.actor.id == "historian"

    research_calls = await env.count(session_factory, ResearchCallRow)
    again = await activities.run_agent_stage(request)
    assert again.replayed and await env.count(session_factory, ResearchCallRow) == research_calls
    assert len(await _rows(session_factory)) == 1
