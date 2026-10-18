"""historian_agent_v1 → FactCheckReport（ADR-0047 §2/§3、INV-58 / INV-60）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agentic import ClaimAssessment, FactCheckReport
from contracts.agents import HISTORIAN_TOOL_REFS, AgentHandlerRef, resolve_agent_spec
from contracts.intervention import AgentRole
from domain.agents.handlers import HANDLERS, needs_llm
from domain.agents.runtime import AGENT_INPUT_NAMES, AgentOutput
from domain.errors import ToolNotAllowedError
from infrastructure.agents.persist import check_output_contract
from tests.support.agent_tools_handlers import (
    INJECTION,
    ScriptedLlm,
    agent_input,
    facts_output,
    make_gateway,
    ok,
    status_only,
)

REF = "search_historical_facts@1"


def _script(**over: Any) -> dict[str, Any]:
    script: dict[str, Any] = {
        "episode_id": "e",
        "type": "script",
        "schema_version": "1.0",
        "language": "ja",
        "title": "Tanegashima",
        "hook": "Guns arrived by accident.",
        "scenes": [
            {"scene_id": "s1", "narration": "Muskets reached Japan in 1543."},
            {"scene_id": "s2", "narration": "The Meiji era began in 1868."},
        ],
    }
    script.update(over)
    return script


def _docs(**over: Any) -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        "script": _script(),
        "user_brief": {"theme": "guns in Japan"},
    }
    docs.update(over)
    return docs


def _claim(text: str, **over: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"claim_text": text, "kind": "year", "importance": "central"}
    c.update(over)
    return c


def _plan(*claims: dict[str, Any], ref: str = REF) -> dict[str, Any]:
    return {"requests": [{"tool_ref": ref, "input": {"claims": list(claims)}}]}


def _executors(**over: Any):
    base = {
        ToolId.SEARCH_HISTORICAL_FACTS: ok(
            facts_output(
                [
                    ("Muskets reached Japan in 1543.", "supported"),
                    ("Meiji began in 1868.", "disputed"),
                ]
            ),
            "research_request:r1",
            limitations=("provider=fake",),
        ),
        ToolId.FETCH_SOURCES: status_only(ToolStatus.NO_DATA),
    }
    base.update(over)
    return base


async def _run(llm: ScriptedLlm, executors, docs=None, *, runtime: bool = True):
    gateway, store = make_gateway(AgentRole.HISTORIAN, "1.0", llm, executors, runtime=runtime)
    inp = agent_input(AgentRole.HISTORIAN, "1.0", docs or _docs())
    out = await HANDLERS[inp.spec.handler_ref](inp, gateway)
    return out, gateway, store


def _report(out: AgentOutput) -> FactCheckReport:
    assert isinstance(out.artifact, FactCheckReport)
    return out.artifact


# --------------------------------------------------------------------------- 登録


def test_the_historian_is_registered_with_its_tools_input_and_output() -> None:
    spec = resolve_agent_spec(AgentRole.HISTORIAN, "1.0")
    assert spec.handler_ref is AgentHandlerRef.HISTORIAN_AGENT_V1
    assert spec.allowed_tool_ids == HISTORIAN_TOOL_REFS
    assert spec.output_schema_ref == "fact_check_report@1.0"
    assert spec.may_create_revisions is False and spec.max_llm_calls <= 3
    assert set(AGENT_INPUT_NAMES[spec.handler_ref]) == {"script", "user_brief"}
    assert HISTORIAN_TOOL_REFS == (REF, "fetch_sources@1")


# --------------------------------------------------------------------------- 往復


async def test_assessments_are_passed_through_as_is_with_evidence_links() -> None:
    llm = ScriptedLlm(
        _plan(_claim("Muskets reached Japan in 1543."), _claim("Meiji began in 1868."))
    )
    out, gateway, store = await _run(llm, _executors())
    report = _report(out)
    assert llm.calls == 1 and store.begins == 1  # 抽出 + 検証の 1 往復。合成の LLM は呼ばない
    assert [c.assessment for c in report.claims] == [
        ClaimAssessment.SUPPORTED,
        ClaimAssessment.DISPUTED,
    ]
    (tool_call_id,) = [i["tool_call_id"] for i in gateway.invocations() if "tool_call_id" in i]
    for claim in report.claims:
        assert claim.evidence_refs[0] == f"tool_call:{tool_call_id}"
        assert "evidence:s1" in claim.evidence_refs
        assert claim.note is not None and claim.note.startswith("reason for")
    assert report.untrusted is True
    assert "provider=fake" in " ".join(report.limitations) or any(
        "fake provider" in lim for lim in report.limitations
    )
    # 提案だけ: 台本を書き換える成果物も storyboard の提案も出さない
    assert out.outcome == "produced" and out.proposals == () and out.findings == ()
    assert out.decision is not None and out.decision.decision_type == "fact_check"
    assert f"tool_call:{tool_call_id}" in out.decision.evidence_refs
    assert "research_request:r1" in out.decision.evidence_refs
    # spec の output_schema_ref どおりの登録済みの版
    spec = resolve_agent_spec(AgentRole.HISTORIAN, "1.0")
    check_output_contract(spec, out)


async def test_insufficient_stays_insufficient_with_no_invented_sources() -> None:
    facts = facts_output([("Muskets reached Japan in 1543.", "insufficient")])
    llm = ScriptedLlm(_plan(_claim("Muskets reached Japan in 1543.")))
    out, _, _ = await _run(
        llm, _executors(**{ToolId.SEARCH_HISTORICAL_FACTS: ok(facts, "research_request:r1")})
    )
    (claim,) = _report(out).claims
    assert claim.assessment is ClaimAssessment.INSUFFICIENT
    assert [r for r in claim.evidence_refs if not r.startswith("tool_call:")] == []


async def test_an_unknown_assessment_is_insufficient_not_mapped_to_a_guess() -> None:
    facts = facts_output([("Muskets reached Japan in 1543.", "probably_true")])
    llm = ScriptedLlm(_plan(_claim("Muskets reached Japan in 1543.")))
    out, _, _ = await _run(llm, _executors(**{ToolId.SEARCH_HISTORICAL_FACTS: ok(facts)}))
    (claim,) = _report(out).claims
    assert claim.assessment is ClaimAssessment.INSUFFICIENT
    assert claim.note is not None and "unrecognized" in claim.note


@pytest.mark.parametrize(
    "status",
    [ToolStatus.PENDING, ToolStatus.PROVIDER_NOT_CONFIGURED, ToolStatus.FAILED, ToolStatus.TIMEOUT],
)
async def test_a_tool_that_could_not_verify_leaves_every_claim_insufficient(status) -> None:
    llm = ScriptedLlm(
        _plan(_claim("Muskets reached Japan in 1543."), _claim("Meiji began in 1868."))
    )
    out, _, _ = await _run(llm, _executors(**{ToolId.SEARCH_HISTORICAL_FACTS: status_only(status)}))
    report = _report(out)
    assert [c.assessment for c in report.claims] == [ClaimAssessment.INSUFFICIENT] * 2
    assert all(c.note is not None and status.value in c.note for c in report.claims)
    assert any(status.value in lim for lim in report.limitations)


async def test_claims_the_tool_did_not_return_are_flagged_not_dropped() -> None:
    facts = facts_output([("Muskets reached Japan in 1543.", "supported")])
    llm = ScriptedLlm(
        _plan(_claim("Muskets reached Japan in 1543."), _claim("Meiji began in 1868."))
    )
    out, _, _ = await _run(llm, _executors(**{ToolId.SEARCH_HISTORICAL_FACTS: ok(facts)}))
    report = _report(out)
    assert [c.assessment for c in report.claims] == [
        ClaimAssessment.SUPPORTED,
        ClaimAssessment.INSUFFICIENT,
    ]
    assert report.claims[1].claim == "Meiji began in 1868."


# --------------------------------------------------------------------------- claim の絞り込み


async def test_at_most_twelve_valid_unique_claims_reach_the_tool() -> None:
    claims = [_claim(f"Claim number {i}.") for i in range(15)]
    claims += [_claim("Claim number 1.")]  # 重複
    claims += [{"claim_text": "", "kind": "year", "importance": "central"}]  # 不正
    claims += [{"claim_text": "x", "kind": "vibes", "importance": "central"}]  # 不正な種類
    executors = _executors()
    llm = ScriptedLlm(_plan(*claims))
    out, _, _ = await _run(llm, executors)
    sent = executors[ToolId.SEARCH_HISTORICAL_FACTS].calls[0][1].claims  # type: ignore[attr-defined]
    assert len(sent) == 12
    assert len({c.claim_text for c in sent}) == 12
    report = _report(out)
    assert any("dropped" in lim or "truncated" in lim for lim in report.limitations)


async def test_extra_fields_from_the_llm_are_not_forwarded_and_cannot_set_an_assessment() -> None:
    executors = _executors()
    llm = ScriptedLlm(_plan(_claim("Muskets reached Japan in 1543.", assessment="supported")))
    await _run(llm, executors)
    sent = executors[ToolId.SEARCH_HISTORICAL_FACTS].calls[0][1].claims[0]  # type: ignore[attr-defined]
    assert not hasattr(sent, "assessment")


async def test_no_valid_claim_means_no_tool_call_and_an_explicit_limitation() -> None:
    executors = _executors()
    llm = ScriptedLlm(_plan({"claim_text": "", "kind": "year", "importance": "central"}))
    out, _, store = await _run(llm, executors)
    report = _report(out)
    assert report.claims == () and store.begins == 0
    assert any("no checkable claim" in lim for lim in report.limitations)


async def test_an_empty_plan_yields_an_empty_report() -> None:
    out, _, store = await _run(ScriptedLlm({"requests": []}), _executors())
    assert _report(out).claims == () and store.begins == 0


# --------------------------------------------------------------------------- 範囲・注入


async def test_only_search_historical_facts_is_used_and_fetch_sources_is_refused() -> None:
    executors = _executors()
    plan = _plan(_claim("Muskets reached Japan in 1543."))
    plan["requests"].append(
        {
            "tool_ref": "fetch_sources@1",
            "input": {"research_request_id": "0" * 8 + "-0000-4000-8000-" + "0" * 12},
        }
    )
    out, _, store = await _run(ScriptedLlm(plan), executors)
    assert store.begins == 1 and not executors[ToolId.FETCH_SOURCES].calls
    assert isinstance(out.artifact, FactCheckReport)


async def test_injection_in_the_script_or_in_a_tool_result_adds_no_call_and_no_edit() -> None:
    hostile_facts = facts_output([("Muskets reached Japan in 1543.", "supported")])
    hostile_facts = hostile_facts.model_copy(
        update={
            "claims": (
                hostile_facts.claims[0].model_copy(update={"assessment_reason": INJECTION[:250]}),
            )
        }
    )
    docs = _docs(script=_script(hook=INJECTION[:70]))
    llm = ScriptedLlm(_plan(_claim("Muskets reached Japan in 1543.")))
    out, _, store = await _run(
        llm, _executors(**{ToolId.SEARCH_HISTORICAL_FACTS: ok(hostile_facts)}), docs
    )
    assert store.begins == 1 and llm.calls == 1
    assert out.proposals == () and _report(out).untrusted is True
    assert llm.prompts[0].count("</data>") == llm.prompts[0].count("<data name=")


async def test_the_report_never_carries_the_script_text() -> None:
    llm = ScriptedLlm(_plan(_claim("Muskets reached Japan in 1543.")))
    out, _, _ = await _run(llm, _executors())
    assert "Guns arrived by accident" not in _report(out).model_dump_json()


# --------------------------------------------------------------------------- 入力欠測・flag


@pytest.mark.parametrize("script", [{}, {"scenes": []}])
async def test_a_missing_script_blocks_without_llm_or_tools(script) -> None:
    docs = _docs(script=script)
    assert needs_llm(AgentHandlerRef.HISTORIAN_AGENT_V1, docs) is False
    llm = ScriptedLlm(_plan(_claim("x")))
    out, gateway, store = await _run(llm, _executors(), docs)
    report = _report(out)
    assert report.claims == () and "blocked_missing_input:script" in report.limitations
    assert out.note == "blocked_missing_input" and llm.calls == 0 and store.begins == 0
    assert gateway.invocations() == []


async def test_a_present_script_needs_the_llm() -> None:
    assert needs_llm(AgentHandlerRef.HISTORIAN_AGENT_V1, _docs()) is True


async def test_without_a_tool_runtime_the_historian_cannot_run_and_costs_nothing() -> None:
    llm = ScriptedLlm(_plan(_claim("x")))
    with pytest.raises(ToolNotAllowedError):
        await _run(llm, _executors(), runtime=False)
    assert llm.calls == 0
