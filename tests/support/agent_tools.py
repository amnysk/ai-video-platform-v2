"""Agent Tool gateway テスト用の fake（実際の tool は持たない。ADR-0047）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from contracts.agent_tools import ToolDef, ToolEnvelope, ToolId, ToolStatus
from contracts.agents import resolve_agent_spec, resolve_model_policy
from contracts.intervention import AgentRole
from domain.agents.ports import LlmResult
from domain.agents.tool_gateway import ToolGateway, ToolRuntime
from domain.agents.tools.observer import ToolEvent
from domain.agents.tools.ports import ToolContext, ToolOutcome
from domain.agents.tools.store import (
    StoredToolCall,
    ToolCallCompletion,
    ToolCallStart,
)

EPISODE = "11111111-1111-4111-8111-111111111111"
RUN_KEY = f"agentic:{EPISODE}:strategy:plan"
SECRET_QUERY = "SECRET-QUERY-DO-NOT-LOG"
SECRET_RESULT = "SECRET-RESULT-DO-NOT-LOG"


class In(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    q: str


class Out(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    answer: str


def tool_def(tool_id: ToolId = ToolId.SEARCH_TRENDS, **kw: Any) -> ToolDef:
    base: dict[str, Any] = {
        "tool_id": tool_id,
        "version": 1,
        "description": "fake",
        "input_model": In,
        "output_model": Out,
        "timeout_seconds": 5,
        "max_calls_per_run": 3,
        "cost_class": "research_ledger",
    }
    base.update(kw)
    return ToolDef(**base)


class MemoryToolCallStore:
    """``ToolCallStore`` の fake。begin/complete の呼び出しを数える。"""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.begins = 0
        self.completes = 0

    async def get(self, tool_call_id: str) -> StoredToolCall | None:
        row = self.rows.get(tool_call_id)
        if row is None:
            return None
        return StoredToolCall(
            tool_call_id=tool_call_id,
            envelope=row.get("envelope"),
            research_request_id=row.get("research_request_id"),
            error_code=row.get("error_code"),
        )

    async def begin(self, start: ToolCallStart) -> None:
        self.begins += 1
        self.rows.setdefault(start.tool_call_id, {"start": start})

    async def complete(self, tool_call_id: str, completion: ToolCallCompletion) -> None:
        self.completes += 1
        row = self.rows[tool_call_id]
        row.update(
            envelope=completion.envelope,
            research_request_id=completion.research_request_id,
            error_code=completion.error_code,
        )


@dataclass
class FakeExecutor:
    outcome: ToolOutcome | None = None
    raises: BaseException | None = None
    sleep: float = 0.0
    calls: list[tuple[ToolContext, BaseModel]] = field(default_factory=list)

    async def __call__(self, ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome:
        self.calls.append((ctx, tool_input))
        if self.sleep:
            import asyncio

            await asyncio.sleep(self.sleep)
        if self.raises is not None:
            raise self.raises
        return self.outcome or ToolOutcome(
            status=ToolStatus.OK,
            data=Out(answer=SECRET_RESULT),
            evidence_refs=("research_request:r1",),
        )


class RecordingObserver:
    def __init__(self, *, explode: bool = False) -> None:
        self.completed_events: list[ToolEvent] = []
        self.refused_events: list[ToolEvent] = []
        self.explode = explode

    def completed(self, event: ToolEvent) -> None:
        if self.explode:
            raise RuntimeError("observer down")
        self.completed_events.append(event)

    def refused(self, event: ToolEvent) -> None:
        if self.explode:
            raise RuntimeError("observer down")
        self.refused_events.append(event)


class _NoLlm:
    async def generate_json(self, *, prompt: str, schema: dict[str, Any], max_tokens: int):
        return LlmResult(data={}, model="fake", input_tokens=1, output_tokens=1)


def make_gateway(
    *,
    executors: dict[ToolId, FakeExecutor] | None = None,
    defs: dict[str, ToolDef] | None = None,
    store: MemoryToolCallStore | None = None,
    observer: RecordingObserver | None = None,
    role: AgentRole = AgentRole.STRATEGY,
    version: str = "1.1",
    run_key: str = RUN_KEY,
    runtime: bool = True,
) -> tuple[ToolGateway, MemoryToolCallStore, dict[ToolId, FakeExecutor]]:
    spec = resolve_agent_spec(role, version)
    store = store if store is not None else MemoryToolCallStore()
    if defs is None:
        trends = tool_def(ToolId.SEARCH_TRENDS, cost_class="research_ledger")
        analytics = tool_def(ToolId.GET_VIDEO_ANALYTICS, cost_class="youtube_quota")
        defs = {trends.ref: trends, analytics.ref: analytics}
    if executors is None:
        executors = {
            ToolId.SEARCH_TRENDS: FakeExecutor(),
            ToolId.GET_VIDEO_ANALYTICS: FakeExecutor(),
        }
    tool_runtime = (
        ToolRuntime(
            defs=defs,
            executors=executors,
            store=store,
            episode_id=EPISODE,
            run_key=run_key,
            observer=observer or RecordingObserver(),
            clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
        )
        if runtime
        else None
    )
    gateway = ToolGateway(
        spec, resolve_model_policy(spec.model_policy_ref), _NoLlm(), {}, tool_runtime
    )
    return gateway, store, executors


def ok_status(envelope: ToolEnvelope) -> bool:
    return envelope.status is ToolStatus.OK
