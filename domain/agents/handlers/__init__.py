"""Agent handler のコード登録（ADR-0043 §2 / §6）。import 文字列や plugin は使わない。

キーは ``contracts.agents.AgentHandlerRef``（全て登録する。``tests/unit/test_agent_handlers.py``）。
"""

from __future__ import annotations

from collections.abc import Mapping

from contracts.agents import AgentHandlerRef
from domain.agents.handlers.creative import creative_director_v1
from domain.agents.handlers.learning import learning_agent_v1
from domain.agents.handlers.quality import quality_reviewer_v1
from domain.agents.handlers.strategy import strategy_agent_v1
from domain.agents.runtime import AgentHandler

HANDLERS: Mapping[AgentHandlerRef, AgentHandler] = {
    AgentHandlerRef.STRATEGY_AGENT_V1: strategy_agent_v1,
    AgentHandlerRef.CREATIVE_DIRECTOR_V1: creative_director_v1,
    AgentHandlerRef.QUALITY_REVIEWER_V1: quality_reviewer_v1,
    AgentHandlerRef.LEARNING_AGENT_V1: learning_agent_v1,
}
