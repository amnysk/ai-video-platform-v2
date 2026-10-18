"""Agent handler の契約（ADR-0043 §6。実装は P1）。

handler は ``(AgentInput, ToolGateway)`` を受け取り ``AgentOutput`` を返す純粋な非同期関数で、
外部へは gateway 経由でしか出ない。出力が契約に合わなければ ``AgentOutputRejected``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from contracts.agentic import QualityFinding
from contracts.agents import AgentHandlerRef, AgentSpec
from contracts.application import StoryboardPatchProposal
from contracts.intervention import DecisionRecordPayload
from domain.agents.tool_gateway import ToolGateway

#: handler ごとの入力 document 名の正本（呼び出し側と handler が同じ名前を使う）
AGENT_INPUT_NAMES: dict[AgentHandlerRef, tuple[str, ...]] = {
    AgentHandlerRef.STRATEGY_AGENT_V1: ("user_brief", "analytics", "lessons", "recent_titles"),
    AgentHandlerRef.CREATIVE_DIRECTOR_V1: ("user_brief", "strategy_brief", "producer_decision"),
    AgentHandlerRef.QUALITY_REVIEWER_V1: (
        "script",
        "storyboard",
        "final_video",
        "rule_review",
        "manifest",
    ),
    AgentHandlerRef.LEARNING_AGENT_V1: ("upload_receipt", "measurements", "baseline"),
    #: ADR-0047: claim の出どころは台本。user_brief は題材の文脈だけ
    AgentHandlerRef.HISTORIAN_AGENT_V1: ("script", "user_brief"),
}

AgentOutcome = Literal["produced", "awaiting_public_metrics", "rejected_output"]


@dataclass(frozen=True, slots=True)
class AgentInput:
    episode_id: str
    stage_id: str
    spec: AgentSpec
    #: ``AGENT_INPUT_NAMES[spec.handler_ref]`` の名前をキーにした JSON object
    documents: Mapping[str, Mapping[str, Any]]
    #: document 名 -> 読んだ revision id
    input_revisions: Mapping[str, str]


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentOutput:
    artifact: BaseModel | None
    decision: DecisionRecordPayload | None
    proposals: tuple[StoryboardPatchProposal, ...] = ()
    findings: tuple[QualityFinding, ...] = ()
    outcome: AgentOutcome
    note: str | None = None


class AgentOutputRejected(Exception):
    """handler の出力が契約に合わない（保存しない）。"""


class AgentHandler(Protocol):
    async def __call__(self, agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput: ...
