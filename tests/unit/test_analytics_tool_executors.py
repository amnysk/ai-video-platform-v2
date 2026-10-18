"""Analytics tool executor（ADR-0047 §3）。理由は docs/testing/agent-tools-rationale.md。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import BaseModel

from contracts.agent_tool_registry import TOOL_DEFS
from contracts.agent_tools import ToolId, ToolStatus
from contracts.agent_tools_analytics import (
    ANALYTICS_TOOL_DEFS,
    ChannelInput,
    CompareInput,
    DemographicsInput,
    RetentionInput,
    VideoAnalyticsInput,
)
from domain.agents.tools.ports import ToolContext
from domain.analytics.ports import InsightResult
from infrastructure.agents.tools import EXECUTOR_BUILDERS
from infrastructure.agents.tools.analytics import build
from infrastructure.config import Settings

VID = "abcdefghijk"
VID2 = "bbcdefghijk"


class FakeBudget:
    def __init__(self) -> None:
        self.used: dict[date, int] = {}

    async def try_consume(self, day: date, limit: int) -> bool:
        if self.used.get(day, 0) >= limit:
            return False
        self.used[day] = self.used.get(day, 0) + 1
        return True


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def _r(self, name: str, *args: Any) -> InsightResult[Any]:
        self.calls.append((name, args))
        return InsightResult(
            status=ToolStatus.NO_DATA,
            limitations=("x",),
            evidence_refs=("analytics_query:abc",),
            retrieved_at="2026-09-20T00:00:00+00:00",
        )

    async def video_analytics(self, *a: Any) -> InsightResult[Any]:
        return self._r("video_analytics", *a)

    async def audience_retention(self, *a: Any) -> InsightResult[Any]:
        return self._r("audience_retention", *a)

    async def compare_videos(self, *a: Any) -> InsightResult[Any]:
        return self._r("compare_videos", *a)

    async def audience_demographics(self, *a: Any) -> InsightResult[Any]:
        return self._r("audience_demographics", *a)

    async def channel_performance(self, *a: Any) -> InsightResult[Any]:
        return self._r("channel_performance", *a)


def ctx(day: date = date(2026, 9, 20)) -> ToolContext:
    return ToolContext(
        tool_call_id="tc",
        episode_id="ep",
        agent_ref="strategy@1.1",
        run_key="rk",
        seq=0,
        idempotency_key="tool:tc",
        now=datetime(day.year, day.month, day.day, 3, tzinfo=UTC),
    )


INPUTS: dict[ToolId, BaseModel] = {
    ToolId.GET_VIDEO_ANALYTICS: VideoAnalyticsInput(video_id=VID),
    ToolId.GET_AUDIENCE_RETENTION: RetentionInput(video_id=VID),
    ToolId.COMPARE_VIDEO_PERFORMANCE: CompareInput(video_ids=(VID, VID2)),
    ToolId.GET_AUDIENCE_DEMOGRAPHICS: DemographicsInput(video_id=VID),
    ToolId.GET_CHANNEL_PERFORMANCE: ChannelInput(),
}


def test_build_returns_exactly_the_five_registered_analytics_tools() -> None:
    ex = build(FakeProvider(), FakeBudget(), daily_cap=5)  # type: ignore[arg-type]
    assert set(ex) == {d.tool_id for d in ANALYTICS_TOOL_DEFS} == set(INPUTS)
    assert all(d.ref in TOOL_DEFS for d in ANALYTICS_TOOL_DEFS)
    assert build in EXECUTOR_BUILDERS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_id", list(INPUTS))
async def test_executor_forwards_to_the_matching_provider_method(tool_id: ToolId) -> None:
    provider = FakeProvider()
    ex = build(provider, FakeBudget(), daily_cap=5)  # type: ignore[arg-type]
    out = await ex[tool_id](ctx(), INPUTS[tool_id])
    assert [c[0] for c in provider.calls] == [
        {
            ToolId.GET_VIDEO_ANALYTICS: "video_analytics",
            ToolId.GET_AUDIENCE_RETENTION: "audience_retention",
            ToolId.COMPARE_VIDEO_PERFORMANCE: "compare_videos",
            ToolId.GET_AUDIENCE_DEMOGRAPHICS: "audience_demographics",
            ToolId.GET_CHANNEL_PERFORMANCE: "channel_performance",
        }[tool_id]
    ]
    assert out.status is ToolStatus.NO_DATA
    assert out.evidence_refs == ("analytics_query:abc",)
    assert out.retrieved_at == "2026-09-20T00:00:00+00:00"
    assert out.extra == {"quota_units": "unknown"}  # 0 ではない


@pytest.mark.asyncio
async def test_daily_cap_blocks_the_call_without_touching_the_provider() -> None:
    provider = FakeProvider()
    budget = FakeBudget()
    ex = build(provider, budget, daily_cap=2)  # type: ignore[arg-type]
    run = ex[ToolId.GET_CHANNEL_PERFORMANCE]
    inp = ChannelInput()
    assert (await run(ctx(), inp)).status is ToolStatus.NO_DATA
    assert (await run(ctx(), inp)).status is ToolStatus.NO_DATA
    third = await run(ctx(), inp)
    assert third.status is ToolStatus.QUOTA_EXCEEDED and third.error_code == "daily_tool_call_cap"
    assert third.extra == {"quota_units": "unknown"}
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_daily_cap_resets_on_the_next_day_and_is_shared_across_tools() -> None:
    provider = FakeProvider()
    ex = build(provider, FakeBudget(), daily_cap=1)  # type: ignore[arg-type]
    d1, d2 = date(2026, 9, 20), date(2026, 9, 21)
    assert (await ex[ToolId.GET_CHANNEL_PERFORMANCE](ctx(d1), ChannelInput())).status is not (
        ToolStatus.QUOTA_EXCEEDED
    )
    blocked = await ex[ToolId.GET_AUDIENCE_RETENTION](ctx(d1), RetentionInput(video_id=VID))
    assert blocked.status is ToolStatus.QUOTA_EXCEEDED
    again = await ex[ToolId.GET_AUDIENCE_RETENTION](ctx(d2), RetentionInput(video_id=VID))
    assert again.status is not ToolStatus.QUOTA_EXCEEDED


@pytest.mark.asyncio
async def test_wrong_input_model_is_a_programming_error_not_a_silent_call() -> None:
    provider = FakeProvider()
    ex = build(provider, FakeBudget(), daily_cap=5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        await ex[ToolId.GET_CHANNEL_PERFORMANCE](ctx(), RetentionInput(video_id=VID))
    assert provider.calls == []


def test_daily_cap_setting_defaults_to_50_and_must_be_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert Settings(_env_file=None).agent_tools_daily_analytics_calls == 50  # type: ignore[call-arg]
    monkeypatch.setenv("AGENT_TOOLS_DAILY_ANALYTICS_CALLS", "7")
    assert Settings(_env_file=None).agent_tools_daily_analytics_calls == 7  # type: ignore[call-arg]
    monkeypatch.setenv("AGENT_TOOLS_DAILY_ANALYTICS_CALLS", "0")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]
