"""Agent tool の executor 実装（ADR-0047）。

``EXECUTOR_BUILDERS``: 各領域の ``build(...)``（``{ToolId: ToolExecutor}`` を返す）の登録先。
組み立ては core（``ToolGateway`` 側）が行う。ここは束ねるだけで、実装を import しない。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from contracts.agent_tools import ToolId
from domain.agents.tools.ports import ToolExecutor

EXECUTOR_BUILDERS: list[Callable[..., Mapping[ToolId, ToolExecutor]]] = []

__all__ = ["EXECUTOR_BUILDERS"]
