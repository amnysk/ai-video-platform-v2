"""承認記録の語彙（ADR-0080）。**唯一の定義**（AGENTS.md §8）。"""

from __future__ import annotations

from enum import StrEnum


class ApproverKind(StrEnum):
    """承認者の種別（ADR-0080）。**唯一の定義**。種別を書かない記録は所有者承認ではない。"""

    OWNER = "owner"
    AGENT_DELEGATED = "agent_delegated"
