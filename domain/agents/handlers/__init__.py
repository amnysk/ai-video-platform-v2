"""Agent handler のコード登録（ADR-0043 §2 / §6）。import 文字列や plugin は使わない。

キーは ``contracts.agents.AgentHandlerRef``（全て登録する。``tests/unit/test_agent_handlers.py``）。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from contracts.agents import AgentHandlerRef
from domain.agents.handlers.creative import creative_director_v1
from domain.agents.handlers.learning import learning_agent_v1, learning_needs_llm
from domain.agents.handlers.quality import quality_reviewer_v1
from domain.agents.handlers.strategy import strategy_agent_v1
from domain.agents.runtime import AgentHandler

HANDLERS: Mapping[AgentHandlerRef, AgentHandler] = {
    AgentHandlerRef.STRATEGY_AGENT_V1: strategy_agent_v1,
    AgentHandlerRef.CREATIVE_DIRECTOR_V1: creative_director_v1,
    AgentHandlerRef.QUALITY_REVIEWER_V1: quality_reviewer_v1,
    AgentHandlerRef.LEARNING_AGENT_V1: learning_agent_v1,
}

#: LLM を呼ばない経路を持つ handler の事前判定（入力 documents だけで決まる決定論）。
#: 未登録 = 常に LLM を呼ぶものとして予約する。``False`` なら予約しない
LLM_PRECHECKS: Mapping[AgentHandlerRef, Callable[[Mapping[str, Mapping[str, Any]]], bool]] = {
    AgentHandlerRef.LEARNING_AGENT_V1: learning_needs_llm,
}


def needs_llm(handler_ref: AgentHandlerRef, documents: Mapping[str, Mapping[str, Any]]) -> bool:
    check = LLM_PRECHECKS.get(handler_ref)
    return True if check is None else check(documents)
