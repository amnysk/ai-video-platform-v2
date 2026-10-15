"""Tool Gateway（ADR-0043 §2、INV-56）。

handler は gateway 経由でしか外部を呼べない。

- ``spec.allowed_tools`` に無い tool、または供給されていない tool は、**何かを呼ぶ前**に
  ``ToolNotAllowedError``
- LLM 呼び出しは ``spec.max_llm_calls`` まで。超過は呼ぶ前に ``AgentBudgetExceededError``
- 呼び出しごとに記録を残す（tool・成否・token・費用。**prompt 本文は残さない**）
- 費用は token usage × policy 単価。欠測は ``None``（0 にしない）
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any

from contracts.agents import AgentSpec, AgentTool, ModelPolicy
from domain.agents.cost import llm_call_cost, sum_costs
from domain.agents.ports import AgentLlm
from domain.errors import AgentBudgetExceededError, ToolNotAllowedError

ToolFn = Callable[..., Awaitable[Any]]


class ToolGateway:
    def __init__(
        self,
        spec: AgentSpec,
        policy: ModelPolicy,
        llm: AgentLlm,
        tools: Mapping[AgentTool, ToolFn],
    ) -> None:
        self._spec = spec
        self._policy = policy
        self._llm = llm
        self._tools = dict(tools)
        self._records: list[dict[str, Any]] = []
        self._llm_calls = 0
        #: LLM を実際に呼んだ回数ぶんの費用（None = 不明）。拒否した呼び出しは含めない
        self._llm_costs: list[Decimal | None] = []
        self._llm_input: list[int | None] = []
        self._llm_output: list[int | None] = []

    # --- 呼び出し -----------------------------------------------------------

    async def llm_json(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        tool = AgentTool.LLM_GENERATE_JSON
        self._require_allowed(tool)
        if self._llm_calls >= self._spec.max_llm_calls:
            self._refuse(tool, "AgentBudgetExceededError")
            raise AgentBudgetExceededError(
                f"{self._spec.role.value}@{self._spec.spec_version} may call the LLM at most "
                f"{self._spec.max_llm_calls} times"
            )
        self._llm_calls += 1
        try:
            result = await self._llm.generate_json(
                prompt=prompt, schema=schema, max_tokens=self._spec.max_tokens
            )
        except Exception as exc:
            # 呼んだ上で失敗した: 使用量・費用は不明（0 にしない）
            self._llm_costs.append(None)
            self._llm_input.append(None)
            self._llm_output.append(None)
            self._records.append(
                {
                    "tool": tool.value,
                    "ok": False,
                    "error": type(exc).__name__,
                    "model": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "cost_usd": None,
                }
            )
            raise
        cost = llm_call_cost(self._policy, result.input_tokens, result.output_tokens)
        self._llm_costs.append(cost)
        self._llm_input.append(result.input_tokens)
        self._llm_output.append(result.output_tokens)
        self._records.append(
            {
                "tool": tool.value,
                "ok": True,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": None if cost is None else str(cost),
            }
        )
        return result.data

    async def call(self, tool: AgentTool, **kwargs: Any) -> Any:
        """LLM 以外の tool。LLM は ``llm_json`` を使う（回数上限と費用の記録のため）。"""
        if tool is AgentTool.LLM_GENERATE_JSON:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError("use llm_json() for llm:generate_json")
        self._require_allowed(tool)
        try:
            result = await self._tools[tool](**kwargs)
        except Exception as exc:
            self._records.append({"tool": tool.value, "ok": False, "error": type(exc).__name__})
            raise
        self._records.append({"tool": tool.value, "ok": True})
        return result

    def _require_allowed(self, tool: AgentTool) -> None:
        if tool not in self._spec.allowed_tools:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError(
                f"{self._spec.role.value}@{self._spec.spec_version} may not use {tool.value}"
            )
        if tool is not AgentTool.LLM_GENERATE_JSON and tool not in self._tools:
            self._refuse(tool, "ToolNotAllowedError")
            raise ToolNotAllowedError(f"tool {tool.value} is not provided to this run")

    def _refuse(self, tool: AgentTool, error: str) -> None:
        self._records.append({"tool": tool.value, "ok": False, "refused": True, "error": error})

    # --- 記録 ---------------------------------------------------------------

    def invocations(self) -> list[dict[str, Any]]:
        """``agent_runs.tool_invocations`` に入れる JSON-safe な記録（prompt 本文を含まない）。"""
        return [dict(r) for r in self._records]

    def token_usage(self) -> dict[str, int] | None:
        """合計 token。どれか 1 回でも usage が無ければ ``None``（測れた分だけを出さない）。"""
        if any(t is None for t in self._llm_input) or any(t is None for t in self._llm_output):
            return None
        return {
            "input_tokens": sum(t for t in self._llm_input if t is not None),
            "output_tokens": sum(t for t in self._llm_output if t is not None),
            "llm_calls": self._llm_calls,
        }

    def actual_cost_usd(self) -> Decimal | None:
        return sum_costs(self._llm_costs)
