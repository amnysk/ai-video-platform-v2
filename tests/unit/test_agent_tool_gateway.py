"""Tool Gateway と費用計算（ADR-0043 §2、INV-56）。外部へは何も出ない。"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import Any

import pytest

from contracts.agents import (
    AGENT_SPECS,
    MODEL_POLICIES,
    AgentTool,
    ModelPolicy,
    resolve_agent_spec,
    resolve_model_policy,
)
from contracts.intervention import AgentRole
from contracts.states import FailureClass
from domain.agents.cost import llm_call_cost, sum_costs
from domain.agents.ports import LlmResult
from domain.agents.tool_gateway import ToolGateway
from domain.errors import (
    AgentBudgetExceededError,
    ToolNotAllowedError,
    classify_failure,
)

PRICED = ModelPolicy("priced", 1, "fake", "m", Decimal("0.002"), Decimal("0.004"))
UNPRICED = resolve_model_policy("codex_cli_text@1")
STRATEGY = resolve_agent_spec(
    AgentRole.STRATEGY, "1.0"
)  # llm + artifact:read + analytics:read, 2 LLM calls


class _Llm:
    def __init__(self, *results: LlmResult | Exception) -> None:
        self._results = list(results)
        self.calls = 0
        self.seen_max_tokens: list[int] = []

    async def generate_json(self, *, prompt: str, schema: dict[str, Any], max_tokens: int):
        self.calls += 1
        self.seen_max_tokens.append(max_tokens)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _ok(i: int | None = 1000, o: int | None = 500) -> LlmResult:
    return LlmResult(data={"a": 1}, model="m", input_tokens=i, output_tokens=o)


async def _read(**kwargs: Any) -> str:
    return "doc"


def test_llm_call_cost_is_token_usage_times_the_unit_price() -> None:
    # 1000 in * 0.002/1k + 500 out * 0.004/1k
    assert llm_call_cost(PRICED, 1000, 500) == Decimal("0.004")


@pytest.mark.parametrize("inp,out", [(None, 5), (5, None), (None, None)])
def test_unknown_usage_gives_none_not_zero(inp, out) -> None:
    assert llm_call_cost(PRICED, inp, out) is None


def test_a_policy_without_prices_cannot_measure_cost() -> None:
    assert llm_call_cost(UNPRICED, 1000, 500) is None


def test_sum_costs_is_none_when_any_part_is_unknown() -> None:
    assert sum_costs([Decimal("1"), Decimal("2")]) == Decimal("3")
    assert sum_costs([Decimal("1"), None]) is None
    assert sum_costs([]) == Decimal("0")


async def test_an_unlisted_tool_is_refused_before_anything_is_called() -> None:
    called: list[str] = []

    async def propose(**kwargs: Any) -> None:
        called.append("propose")

    llm = _Llm()
    gateway = ToolGateway(STRATEGY, PRICED, llm, {AgentTool.INTERVENTION_PROPOSE: propose})
    assert AgentTool.INTERVENTION_PROPOSE not in STRATEGY.allowed_tools
    with pytest.raises(ToolNotAllowedError):
        await gateway.call(AgentTool.INTERVENTION_PROPOSE, x=1)
    assert called == [] and llm.calls == 0
    assert gateway.invocations()[0]["refused"] is True


async def test_a_listed_but_unprovided_tool_is_refused() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(), {})
    assert AgentTool.ARTIFACT_READ in STRATEGY.allowed_tools
    with pytest.raises(ToolNotAllowedError):
        await gateway.call(AgentTool.ARTIFACT_READ, name="x")


async def test_the_llm_tool_cannot_go_through_call() -> None:
    llm = _Llm(_ok())
    gateway = ToolGateway(STRATEGY, PRICED, llm, {})
    with pytest.raises(ToolNotAllowedError):
        await gateway.call(AgentTool.LLM_GENERATE_JSON)
    assert llm.calls == 0


async def test_llm_json_is_refused_when_the_spec_does_not_list_it() -> None:
    spec = dataclasses.replace(STRATEGY, allowed_tools=(AgentTool.ARTIFACT_READ,))
    llm = _Llm(_ok())
    with pytest.raises(ToolNotAllowedError):
        await ToolGateway(spec, PRICED, llm, {}).llm_json("p", {})
    assert llm.calls == 0


async def test_the_llm_call_cap_is_enforced_before_the_next_call() -> None:
    llm = _Llm(_ok(), _ok(), _ok())
    gateway = ToolGateway(STRATEGY, PRICED, llm, {})
    assert STRATEGY.max_llm_calls == 2
    await gateway.llm_json("p1", {})
    await gateway.llm_json("p2", {})
    with pytest.raises(AgentBudgetExceededError):
        await gateway.llm_json("p3", {})
    assert llm.calls == 2
    assert llm.seen_max_tokens == [STRATEGY.max_tokens] * 2


def test_gateway_errors_are_permanent_failures() -> None:
    assert classify_failure(ToolNotAllowedError("x")) is FailureClass.PERMANENT
    assert classify_failure(AgentBudgetExceededError("x")) is FailureClass.PERMANENT


async def test_a_measured_run_reports_tokens_and_cost() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(_ok(1000, 500), _ok(2000, 0)), {})
    await gateway.llm_json("p1", {})
    await gateway.llm_json("p2", {})
    assert gateway.token_usage() == {"input_tokens": 3000, "output_tokens": 500, "llm_calls": 2}
    # 0.004 + (2000*0.002/1000 = 0.004)
    assert gateway.actual_cost_usd() == Decimal("0.008")


async def test_unknown_usage_makes_tokens_and_cost_none_not_zero() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(_ok(1000, 500), _ok(None, None)), {})
    await gateway.llm_json("p1", {})
    await gateway.llm_json("p2", {})
    assert gateway.token_usage() is None
    assert gateway.actual_cost_usd() is None
    assert [r["cost_usd"] for r in gateway.invocations()] == ["0.004", None]


async def test_an_unpriced_policy_gives_none_cost_but_known_tokens() -> None:
    gateway = ToolGateway(STRATEGY, UNPRICED, _Llm(_ok()), {})
    await gateway.llm_json("p", {})
    assert gateway.actual_cost_usd() is None
    assert gateway.token_usage() == {"input_tokens": 1000, "output_tokens": 500, "llm_calls": 1}


async def test_a_failed_llm_call_counts_against_the_cap_and_makes_cost_unknown() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(RuntimeError("boom"), _ok()), {})
    with pytest.raises(RuntimeError):
        await gateway.llm_json("p1", {})
    await gateway.llm_json("p2", {})
    assert gateway.actual_cost_usd() is None
    assert gateway.token_usage() is None
    assert [r["ok"] for r in gateway.invocations()] == [False, True]


async def test_invocations_are_json_safe_and_never_contain_the_prompt() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(_ok()), {AgentTool.ARTIFACT_READ: _read})
    await gateway.llm_json("SECRET-PROMPT-TEXT", {"title": "S"})
    assert await gateway.call(AgentTool.ARTIFACT_READ, name="x") == "doc"
    dumped = json.dumps(gateway.invocations())
    assert "SECRET-PROMPT-TEXT" not in dumped
    assert [r["tool"] for r in gateway.invocations()] == ["llm:generate_json", "artifact:read"]


async def test_a_run_without_llm_calls_has_a_known_zero() -> None:
    gateway = ToolGateway(STRATEGY, PRICED, _Llm(), {AgentTool.ARTIFACT_READ: _read})
    await gateway.call(AgentTool.ARTIFACT_READ, name="x")
    assert gateway.actual_cost_usd() == Decimal("0")
    assert gateway.token_usage() == {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0}


def test_registered_specs_reference_registered_policies() -> None:
    assert all(s.model_policy_ref in MODEL_POLICIES for s in AGENT_SPECS.values())
