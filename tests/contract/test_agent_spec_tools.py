"""AgentSpec.allowed_tool_ids（ADR-0047 §2、INV-58 / INV-51）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

from dataclasses import replace

from contracts.agent_tools import parse_tool_ref
from contracts.agents import (
    AGENT_SPECS,
    HISTORIAN_TOOL_REFS,
    TOOL_STAGE_MAX_LLM_CALLS,
    resolve_agent_spec,
)
from contracts.intervention import AgentRole

#: ADR-0047 §2 の許可表（literal。実装の定数から導出しない）
EXPECTED = {
    (AgentRole.STRATEGY, "1.1"): {
        "search_trends@1",
        "get_video_analytics@1",
        "compare_video_performance@1",
        "get_audience_demographics@1",
        "get_channel_performance@1",
    },
    (AgentRole.LEARNING, "1.1"): {
        "get_video_analytics@1",
        "get_audience_retention@1",
        "compare_video_performance@1",
    },
}


def test_v1_0_specs_have_no_tools_and_omit_the_key_from_canonical() -> None:
    for (role, version), spec in AGENT_SPECS.items():
        if version == "1.0":
            assert spec.allowed_tool_ids == ()
            assert "allowed_tool_ids" not in spec.canonical(), role


def test_the_new_specs_carry_exactly_the_adr_tool_lists() -> None:
    for key, refs in EXPECTED.items():
        spec = AGENT_SPECS[key]
        assert set(spec.allowed_tool_ids) == refs
        assert len(set(spec.allowed_tool_ids)) == len(spec.allowed_tool_ids)
        assert spec.canonical()["allowed_tool_ids"] == list(spec.allowed_tool_ids)


def test_every_tool_ref_parses_to_a_declared_tool_id() -> None:
    for spec in AGENT_SPECS.values():
        for ref in spec.allowed_tool_ids:
            parse_tool_ref(ref)
    for ref in HISTORIAN_TOOL_REFS:
        parse_tool_ref(ref)


def test_tool_specs_stay_within_one_round_trip_of_llm_calls() -> None:
    assert TOOL_STAGE_MAX_LLM_CALLS <= 3
    for spec in AGENT_SPECS.values():
        if spec.allowed_tool_ids:
            assert 1 <= spec.max_llm_calls <= 3


def test_changing_the_tool_list_changes_the_hash_and_empty_changes_nothing() -> None:
    spec = resolve_agent_spec(AgentRole.STRATEGY, "1.1")
    assert replace(spec, allowed_tool_ids=spec.allowed_tool_ids[:-1]).manifest_hash != (
        spec.manifest_hash
    )
    v10 = resolve_agent_spec(AgentRole.STRATEGY, "1.0")
    assert replace(v10, allowed_tool_ids=()).manifest_hash == v10.manifest_hash


def test_v1_1_differs_from_v1_0_only_in_version_budget_and_tools() -> None:
    for role in (AgentRole.STRATEGY, AgentRole.LEARNING):
        old, new = resolve_agent_spec(role, "1.0"), resolve_agent_spec(role, "1.1")
        a, b = old.canonical(), new.canonical()
        diff = {k for k in {*a, *b} if a.get(k) != b.get(k)}
        assert diff == {"spec_version", "max_llm_calls", "allowed_tool_ids"}
