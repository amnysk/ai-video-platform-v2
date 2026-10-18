"""strategy_agent_v1 の spec 1.1（tool あり。ADR-0047 §2/§3、INV-57 / INV-58 / INV-60）。

fake LLM + 本物の spec/registry + fake executor。外へは出ない。
理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agentic import StrategyBrief
from contracts.intervention import AgentRole
from contracts.topic_planning import STRATEGY_PROFILES
from domain.agents.handlers import HANDLERS
from domain.agents.handlers.strategy import ASSUMED_AUDIENCE_NOTE
from domain.agents.runtime import AgentOutputRejected
from domain.errors import ToolNotAllowedError
from tests.support.agent_tools_handlers import (
    INJECTION,
    ScriptedLlm,
    agent_input,
    channel_output,
    make_gateway,
    ok,
    status_only,
    trends_output,
)

ERA = STRATEGY_PROFILES["us_young_history_v1"].eras[0]
UNKNOWN_ID = "00000000-0000-4000-8000-00000000dead"

PLAN = {
    "requests": [
        {
            "tool_ref": "search_trends@1",
            "input": {
                "region": "US",
                "audience_hypothesis": "US 20s history fans",
                "seed_terms": ["samurai"],
            },
        },
        {"tool_ref": "get_channel_performance@1", "input": {}},
    ]
}


def _cand(cid: str, title: str, **over: Any) -> dict[str, Any]:
    c = {
        "id": cid,
        "title": title,
        "subject": f"subject_{cid}",
        "angle": "reason",
        "era": ERA,
        "hook": "What if everything you know is wrong?",
        "rationale": "Surprising concrete detail.",
        "risks": ["niche"],
    }
    c.update(over)
    return c


def _answer(**over: Any) -> dict[str, Any]:
    a: dict[str, Any] = {
        "audience_hypothesis": "Curious young viewers like concrete oddities.",
        "candidates": [
            _cand("c1", "Why samurai wore silk"),
            _cand("c2", "Castle toilets explained"),
        ],
        "recommended_candidate_id": "c1",
        "success_criteria": ["retention above baseline"],
        "citations": [],
        "uncertainties": ["trend unknown"],
        "rationale": "Strongest hook.",
    }
    a.update(over)
    return a


def _docs(**over: Any) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        "user_brief": {
            "theme": "samurai daily life",
            "template_id": "shorts_agentic_tools",
            "template_version": "1",
        },
        "analytics": {"measured": False, "videos_with_views": 0},
        "lessons": {"items": []},
        "recent_titles": {"titles": []},
    }
    docs.update(over)
    return docs


def _executors(**over: Any):
    base: dict[ToolId, Any] = {
        ToolId.SEARCH_TRENDS: ok(
            trends_output(), "research_request:r1", limitations=("provider=fake",)
        ),
        ToolId.GET_CHANNEL_PERFORMANCE: ok(channel_output(), "analytics_query:abc"),
    }
    base.update(over)  # type: ignore[call-overload]  # key は ToolId.value の文字列
    return base


async def _run(llm: ScriptedLlm, executors, docs=None, *, runtime: bool = True):
    gateway, store = make_gateway(AgentRole.STRATEGY, "1.1", llm, executors, runtime=runtime)
    inp = agent_input(AgentRole.STRATEGY, "1.1", docs or _docs())
    out = await HANDLERS[inp.spec.handler_ref](inp, gateway)
    return out, gateway, store


def _ids(gateway) -> list[str]:
    return [i["tool_call_id"] for i in gateway.invocations() if i.get("tool_call_id")]


# --------------------------------------------------------------------------- 往復と証拠の連結


async def test_a_tool_round_produces_a_valid_brief_citing_the_executed_tool_calls() -> None:
    llm = ScriptedLlm(PLAN, _answer())
    out, gateway, store = await _run(llm, _executors())
    brief = out.artifact
    assert isinstance(brief, StrategyBrief) and brief.schema_version == "1.0"
    ids = _ids(gateway)
    assert len(ids) == 2 and store.begins == 2
    for tool_call_id in ids:
        assert f"tool_call:{tool_call_id}" in brief.citations
    assert llm.calls == 2  # plan + 合成
    # 判断記録にも同じ ID と、tool が返した証拠参照が載る
    decision = out.decision
    assert decision is not None
    for tool_call_id in ids:
        assert f"tool_call:{tool_call_id}" in decision.evidence_refs
    assert "research_request:r1" in decision.evidence_refs
    assert "analytics_query:abc" in decision.evidence_refs


async def test_the_synthesis_prompt_carries_results_only_inside_data_blocks() -> None:
    llm = ScriptedLlm(PLAN, _answer())
    await _run(llm, _executors())
    plan_prompt, synth_prompt = llm.prompts
    assert "search_trends@1" in plan_prompt and "get_channel_performance@1" in plan_prompt
    assert '<data name="tool:search_trends">' in synth_prompt
    assert '<data name="tool:get_channel_performance">' in synth_prompt
    assert "tool_call:" in synth_prompt  # id を引用できる
    # 候補に出るのは spec が許し、かつ executor が供給された tool だけ
    assert "get_video_analytics" not in plan_prompt


async def test_a_run_with_an_empty_plan_still_produces_a_brief_without_tool_rows() -> None:
    llm = ScriptedLlm({"requests": []}, _answer())
    out, gateway, store = await _run(llm, _executors())
    assert isinstance(out.artifact, StrategyBrief)
    assert store.begins == 0 and _ids(gateway) == [] and llm.calls == 2
    assert out.artifact.citations == ()


# --------------------------------------------------------------------------- 引用の検証


async def test_unknown_tool_call_citations_are_dropped_and_recorded() -> None:
    # tool_call_id は run の冪等キー・順序・入力から決まる。同じ計画をもう一度走らせて実 ID を知る
    _, probe, _ = await _run(ScriptedLlm(PLAN, _answer()), _executors())
    real = _ids(probe)[0]
    answer = _answer(
        citations=[f"tool_call:{real}", f"tool_call:{UNKNOWN_ID}", "analytics:2026-09"],
        candidates=[
            _cand(
                "c1",
                "Why samurai wore silk",
                rationale=f"Trend seen in tool_call:{UNKNOWN_ID} and tool_call:{real}.",
            ),
            _cand("c2", "Castle toilets explained"),
        ],
        rationale=f"Because of tool_call:{UNKNOWN_ID}.",
    )
    out, gateway, _ = await _run(ScriptedLlm(PLAN, answer), _executors())
    assert _ids(gateway)[0] == real
    brief = out.artifact
    assert isinstance(brief, StrategyBrief)
    assert not any(UNKNOWN_ID in c for c in brief.citations)
    assert f"tool_call:{real}" in brief.citations
    assert "analytics:2026-09" in brief.citations  # tool_call 以外の自由な引用は 1.0 と同じ扱い
    assert UNKNOWN_ID not in brief.candidates[0].rationale
    assert f"tool_call:{real}" in brief.candidates[0].rationale  # 実在する引用は残る
    assert out.decision is not None and UNKNOWN_ID not in (out.decision.rationale or "")
    assert any("citation" in u.lower() for u in brief.uncertainties)


# --------------------------------------------------------------- 取得できないものを作らない


async def test_unavailable_analytics_yields_a_brief_with_uncertainties_and_assumed_audience() -> (
    None
):
    llm = ScriptedLlm(PLAN, _answer())
    out, gateway, _ = await _run(
        llm,
        _executors(
            **{
                ToolId.GET_CHANNEL_PERFORMANCE: status_only(ToolStatus.PERMISSION_DENIED),
            }
        ),
    )
    brief = out.artifact
    assert isinstance(brief, StrategyBrief)
    assert brief.audience_basis == "assumed"
    assert ASSUMED_AUDIENCE_NOTE in brief.uncertainties
    assert any(
        "get_channel_performance" in u and "permission_denied" in u for u in brief.uncertainties
    )
    # 失敗した呼び出しも「使った tool call」として辿れる
    assert len([c for c in brief.citations if c.startswith("tool_call:")]) == 2


async def test_a_pending_trend_is_a_note_and_the_fake_provider_is_disclosed() -> None:
    llm = ScriptedLlm(PLAN, _answer())
    out, _, _ = await _run(
        llm, _executors(**{ToolId.SEARCH_TRENDS: status_only(ToolStatus.PENDING)})
    )
    assert isinstance(out.artifact, StrategyBrief)
    assert any("search_trends" in u and "pending" in u for u in out.artifact.uncertainties)
    llm2 = ScriptedLlm(PLAN, _answer())
    out2, _, _ = await _run(llm2, _executors())
    assert isinstance(out2.artifact, StrategyBrief)
    assert any("fake provider" in u for u in out2.artifact.uncertainties)


async def test_audience_basis_never_comes_from_the_llm_or_from_trend_hypotheses() -> None:
    # tool が全部 ok でも、analytics document が実測でなければ assumed
    llm = ScriptedLlm(PLAN, _answer(audience_basis="measured"))
    out, _, _ = await _run(llm, _executors())
    assert isinstance(out.artifact, StrategyBrief) and out.artifact.audience_basis == "assumed"
    # 実測（十分な動画数の analytics snapshot）なら measured。tool が失敗しても変わらない
    measured = _docs(analytics={"measured": True, "videos_with_views": 12})
    llm2 = ScriptedLlm(PLAN, _answer())
    out2, _, _ = await _run(
        llm2,
        _executors(**{ToolId.GET_CHANNEL_PERFORMANCE: status_only(ToolStatus.FAILED)}),
        measured,
    )
    assert isinstance(out2.artifact, StrategyBrief) and out2.artifact.audience_basis == "measured"


# --------------------------------------------------------------------------- 注入・再帰・上限


async def test_injection_in_a_tool_result_cannot_add_calls_or_change_fields() -> None:
    hostile = ok(trends_output(theme_text=INJECTION), "research_request:r1")
    llm = ScriptedLlm(PLAN, _answer())
    out, gateway, store = await _run(llm, _executors(**{ToolId.SEARCH_TRENDS: hostile}))
    assert isinstance(out.artifact, StrategyBrief)
    assert store.begins == 2 and llm.calls == 2  # 計画の 2 件だけ。「もう一度呼べ」は無視
    assert out.artifact.audience_basis == "assumed"
    assert out.artifact.recommended_candidate_id == "c1"
    synth = llm.prompts[1]
    assert INJECTION.split(".")[0] in synth  # 結果は DATA として渡る
    assert synth.count("</data>") == synth.count("<data name=")  # 区切りを閉じられない
    assert out.proposals == () and out.findings == ()


async def test_a_plan_with_disallowed_or_excess_tools_is_cut_down_without_external_calls() -> None:
    plan = {
        "requests": [
            {"tool_ref": "search_historical_facts@1", "input": {"claims": []}},  # 許可外
            {"tool_ref": "post_video@1", "input": {}},  # 存在しない
            *[
                {"tool_ref": "get_channel_performance@1", "input": {"start_date": f"2026-09-0{i}"}}
                for i in range(1, 5)
            ],  # 上限 2 を超える
        ]
    }
    llm = ScriptedLlm(plan, _answer())
    out, _, store = await _run(llm, _executors())
    assert isinstance(out.artifact, StrategyBrief)
    assert store.begins == 2  # get_channel_performance の上限（2）まで
    assert any("refused" in u for u in out.artifact.uncertainties)


async def test_a_synthesis_violation_is_repaired_once_within_three_llm_calls() -> None:
    llm = ScriptedLlm(PLAN, {"candidates": "nope"}, _answer())
    out, gateway, _ = await _run(llm, _executors())
    assert isinstance(out.artifact, StrategyBrief) and llm.calls == 3
    assert gateway.token_usage() == {"input_tokens": 3, "output_tokens": 3, "llm_calls": 3}


async def test_two_synthesis_violations_are_rejected() -> None:
    llm = ScriptedLlm(PLAN, {"candidates": "nope"}, {"candidates": "still nope"}, _answer())
    with pytest.raises(AgentOutputRejected):
        await _run(llm, _executors())
    assert llm.calls == 3


async def test_without_a_tool_runtime_the_1_1_spec_cannot_run_and_costs_nothing() -> None:
    llm = ScriptedLlm(PLAN, _answer())
    with pytest.raises(ToolNotAllowedError):
        await _run(llm, _executors(), runtime=False)
    assert llm.calls == 0


async def test_v1_0_never_plans_even_if_a_tool_runtime_exists() -> None:
    from contracts.agents import resolve_agent_spec, resolve_model_policy
    from domain.agents.tool_gateway import ToolGateway

    llm = ScriptedLlm(_answer())
    spec = resolve_agent_spec(AgentRole.STRATEGY, "1.0")
    gateway = ToolGateway(spec, resolve_model_policy(spec.model_policy_ref), llm, {})
    inp = agent_input(AgentRole.STRATEGY, "1.0", _docs())
    out = await HANDLERS[spec.handler_ref](inp, gateway)
    assert isinstance(out.artifact, StrategyBrief) and llm.calls == 1
    assert "tool_call" not in llm.prompts[0]
