"""Agent Tool の executor 群（ADR-0047 §3）。

``EXECUTOR_BUILDERS`` の各 ``build`` は ``{ToolId: ToolExecutor}`` を返す。
引数は builder ごとに違う（依存の注入）ので、組み立ては呼び出し側（起動処理）が行う。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from contracts.agent_tools import ToolId
from domain.agents.tools.ports import ToolExecutor
from infrastructure.agents.tools import analytics, research

EXECUTOR_BUILDERS: list[Callable[..., Mapping[ToolId, ToolExecutor]]] = [
    research.build,
    analytics.build,
]

__all__: list[str] = ["EXECUTOR_BUILDERS"]
