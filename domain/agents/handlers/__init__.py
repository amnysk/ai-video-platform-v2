"""Agent handler のコード登録（ADR-0043 §2）。import 文字列や plugin は使わない。

キーは ``contracts.agents.AgentHandlerRef``。P1 で4つの handler を登録する。
"""

from __future__ import annotations

from collections.abc import Mapping

from contracts.agents import AgentHandlerRef
from domain.agents.runtime import AgentHandler

HANDLERS: Mapping[AgentHandlerRef, AgentHandler] = {}
