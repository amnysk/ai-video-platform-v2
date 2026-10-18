"""tool executor の組み立てと日次呼び出し上限（ADR-0047 §3/§5）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from contracts.agent_tools import ToolId
from domain.agents.tools.store import ToolCallStart
from infrastructure.agents.tools.budget import AgentToolCallBudget
from infrastructure.db.agent_tool_call_repository import AgentToolCallRepository
from infrastructure.db.models import EpisodeRow
from tests.support.agent_tools import EPISODE, RUN_KEY

DAY = date(2026, 10, 18)


def _start(seq: int, tool: str, when: datetime) -> ToolCallStart:
    return ToolCallStart(
        tool_call_id=str(uuid.uuid4()),
        run_key=RUN_KEY,
        episode_id=EPISODE,
        agent_role="strategy",
        agent_spec_version="1.1",
        seq=seq,
        tool_id=tool,
        tool_version=1,
        input_sha256="a" * 64,
        tool_input={},
        started_at=when,
    )


async def _seed(session_factory) -> None:
    async with session_factory() as s:
        s.add(EpisodeRow(id=uuid.UUID(EPISODE), status="planned"))
        await s.commit()


async def test_the_daily_cap_counts_only_analytics_tool_calls_of_that_day(
    session_factory, artifact_store
) -> None:
    """日次上限は analytics の呼び出しだけを数える（research や別の日は数えない）。
    gateway は executor の前に行を begin するので、今回の分を含めて上限と比べる。"""
    await _seed(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    today = datetime(2026, 10, 18, 3, tzinfo=UTC)
    await repo.begin(_start(1, ToolId.SEARCH_TRENDS.value, today))
    await repo.begin(
        _start(2, ToolId.GET_VIDEO_ANALYTICS.value, datetime(2026, 10, 17, tzinfo=UTC))
    )
    budget = AgentToolCallBudget(session_factory)

    await repo.begin(_start(3, ToolId.GET_VIDEO_ANALYTICS.value, today))  # 今日の 1 回目
    assert await budget.try_consume(DAY, 2) is True
    await repo.begin(_start(4, ToolId.GET_CHANNEL_PERFORMANCE.value, today))  # 2 回目
    assert await budget.try_consume(DAY, 2) is True
    await repo.begin(_start(5, ToolId.GET_AUDIENCE_RETENTION.value, today))  # 3 回目 > 2
    assert await budget.try_consume(DAY, 2) is False


def _ctx(session_factory, artifact_store, **extras):
    from infrastructure.agents.tools import ToolBuildContext
    from infrastructure.config import Settings

    settings = Settings(youtube_channel_id="UC1", agent_tools_daily_analytics_calls=3)
    return ToolBuildContext(settings, session_factory, artifact_store, "b", extras)


def test_executors_are_supplied_only_when_their_adapters_are_injected(
    session_factory, artifact_store
) -> None:
    """adapter が注入されない tool 群は executor を持たない。

    gateway が拒否する。黙って偽の結果を返さない。
    """
    from infrastructure.agents.tools import (
        EXTRA_INSIGHT_PROVIDER,
        EXTRA_RESEARCH_STARTER,
        _analytics_executors,
        _research_executors,
    )

    assert _research_executors(_ctx(session_factory, artifact_store)) == {}
    assert _analytics_executors(_ctx(session_factory, artifact_store)) == {}
    r = _research_executors(
        _ctx(session_factory, artifact_store, **{EXTRA_RESEARCH_STARTER: object()})
    )
    assert set(r) == {
        ToolId.SEARCH_TRENDS,
        ToolId.SEARCH_HISTORICAL_FACTS,
        ToolId.FETCH_SOURCES,
    }
    a = _analytics_executors(
        _ctx(session_factory, artifact_store, **{EXTRA_INSIGHT_PROVIDER: object()})
    )
    assert set(a) == {
        ToolId.GET_VIDEO_ANALYTICS,
        ToolId.GET_AUDIENCE_RETENTION,
        ToolId.COMPARE_VIDEO_PERFORMANCE,
        ToolId.GET_AUDIENCE_DEMOGRAPHICS,
        ToolId.GET_CHANNEL_PERFORMANCE,
    }


async def test_extras_leave_out_what_cannot_be_built() -> None:
    """OAuth 設定が無い・analytics が無効なら insight provider を作らない（捏造しない）。"""
    import httpx

    from infrastructure.agents.tools.extras import build_tool_extras
    from infrastructure.config import Settings

    async with httpx.AsyncClient() as http:
        off = build_tool_extras(Settings(), temporal_client=None, http_client=http)
        incomplete = build_tool_extras(
            Settings(youtube_analytics_enabled=True), temporal_client=None, http_client=http
        )
    assert off == {} and incomplete == {}
