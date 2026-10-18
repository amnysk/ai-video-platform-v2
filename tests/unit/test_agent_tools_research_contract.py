"""Research tool の入出力契約（ADR-0047 §4）。理由: docs/testing/agent-tools-rationale.md"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.agent_tool_registry import TOOL_DEFS, resolve_tool
from contracts.agent_tools import ToolEffect, ToolId
from contracts.agent_tools_research import (
    FetchSourcesInput,
    SearchHistoricalFactsInput,
    SearchTrendsInput,
)

RESEARCH_IDS = (ToolId.SEARCH_TRENDS, ToolId.SEARCH_HISTORICAL_FACTS, ToolId.FETCH_SOURCES)
CLAIM = {"claim_text": "鉄砲伝来は1543年とされる", "kind": "year", "importance": "central"}


def test_the_three_research_tools_are_registered_read_only_with_ledger_cost() -> None:
    for tool_id in RESEARCH_IDS:
        d = resolve_tool(f"{tool_id.value}@1")
        assert d.effect is ToolEffect.READ
        assert d.cost_class == "research_ledger"
        assert d.timeout_seconds >= 1 and d.max_calls_per_run >= 1
        assert d.input_schema()["additionalProperties"] is False
        assert d.output_schema()["additionalProperties"] is False
    assert {d.tool_id for d in TOOL_DEFS.values()} >= set(RESEARCH_IDS)


def test_search_trends_input_is_strict() -> None:
    ok = SearchTrendsInput.model_validate(
        {"region": "JP", "seed_terms": ["関ヶ原"], "audience_hypothesis": "20代の歴史好き"}
    )
    assert ok.seed_terms == ("関ヶ原",)
    for bad in (
        {"region": "jp", "audience_hypothesis": "x"},
        {"region": "JP", "audience_hypothesis": ""},
        {"region": "JP", "audience_hypothesis": "x" * 301},
        {"region": "JP", "audience_hypothesis": "x", "seed_terms": ["a"] * 11},
        {"region": "JP", "audience_hypothesis": "x", "seed_terms": ["y" * 101]},
        {"region": "JP", "audience_hypothesis": "x", "unknown": 1},
    ):
        with pytest.raises(ValidationError):
            SearchTrendsInput.model_validate(bad)


def test_historical_facts_input_limits_claims_to_12_of_300_chars() -> None:
    SearchHistoricalFactsInput.model_validate({"claims": [CLAIM]})
    many = [{**CLAIM, "claim_text": f"claim {i}"} for i in range(13)]
    for bad in (
        {"claims": []},
        {"claims": many},
        {"claims": [{**CLAIM, "claim_text": "x" * 301}]},
        {"claims": [CLAIM, CLAIM]},
        {"claims": [{**CLAIM, "kind": "nonsense"}]},
    ):
        with pytest.raises(ValidationError):
            SearchHistoricalFactsInput.model_validate(bad)


def test_fetch_sources_input_needs_a_reference_and_accepts_a_url_only_to_refuse_it() -> None:
    rid = "11111111-1111-4111-8111-111111111111"
    assert FetchSourcesInput.model_validate({"research_request_id": rid}).url is None
    assert FetchSourcesInput.model_validate({"url": "https://a.example/x"}).url
    with pytest.raises(ValidationError):
        FetchSourcesInput.model_validate({})
    with pytest.raises(ValidationError):
        FetchSourcesInput.model_validate({"research_request_id": "not-a-uuid"})
