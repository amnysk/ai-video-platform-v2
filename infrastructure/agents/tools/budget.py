"""Analytics tool の日次呼び出し回数（ADR-0047 §3）。

新しい表を作らず、``agent_tool_calls`` の当日の analytics 行を数える。``ToolGateway`` は
executor を呼ぶ前に行を ``begin`` するので、数えた値には**今回の呼び出しも含まれる**
（``count <= limit`` なら許可）。厳密な排他ではなく、並行する run が上限を数件超えうる
（quota 単価が不明なので、回数の上限は目安であって保証ではない）。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agent_tool_registry import TOOL_DEFS
from infrastructure.db.models import AgentToolCallRow


def _analytics_tool_ids() -> list[str]:
    return sorted({d.tool_id.value for d in TOOL_DEFS.values() if d.cost_class == "youtube_quota"})


class AgentToolCallBudget:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def try_consume(self, day: date, limit: int) -> bool:
        start = datetime.combine(day, time.min, tzinfo=UTC)
        async with self._sf() as session:
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(AgentToolCallRow)
                    .where(
                        AgentToolCallRow.tool_id.in_(_analytics_tool_ids()),
                        AgentToolCallRow.started_at >= start,
                        AgentToolCallRow.started_at < start + timedelta(days=1),
                    )
                )
            ).scalar_one()
        return int(count) <= limit
