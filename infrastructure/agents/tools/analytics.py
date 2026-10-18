"""YouTube Analytics tool の executor（ADR-0047 §3 / §5）。

外部呼び出し（``VideoInsightProvider``）と日次の呼び出し回数の上限だけを担当する。
tool_call_id・保存済み結果の再利用・timeout・記録は ``ToolGateway`` の仕事。

- 上限は呼び出し単位（HTTP 単位ではない）。超過は ``quota_exceeded``（``daily_tool_call_cap``）で、
  外部を呼ばない。quota 単価は未確認なので ``quota_units=unknown`` を記録する（0 にしない）
- 結果の ``untrusted`` 化・``<data>`` 区切りは gateway / envelope 側
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel

from contracts.agent_tools import ToolId, ToolStatus
from contracts.agent_tools_analytics import (
    ChannelInput,
    CompareInput,
    DemographicsInput,
    RetentionInput,
    VideoAnalyticsInput,
)
from domain.agents.tools.ports import ToolContext, ToolExecutor, ToolOutcome
from domain.analytics.ports import CallBudget, InsightResult, VideoInsightProvider

QUOTA_UNITS_UNKNOWN = {"quota_units": "unknown"}


def _outcome(result: InsightResult[Any]) -> ToolOutcome:
    return ToolOutcome(
        status=result.status,
        data=result.data,
        limitations=result.limitations,
        evidence_refs=result.evidence_refs,
        retrieved_at=result.retrieved_at,
        error_code=result.error_code,
        extra={"quota_units": result.quota_units},
    )


def _executor[M: BaseModel](
    model: type[M],
    budget: CallBudget,
    daily_cap: int,
    call: Callable[[M], Awaitable[InsightResult[Any]]],
) -> ToolExecutor:
    async def execute(ctx: ToolContext, tool_input: BaseModel) -> ToolOutcome:
        if not isinstance(tool_input, model):
            raise TypeError(f"expected {model.__name__}")
        if not await budget.try_consume(ctx.now.date(), daily_cap):
            return ToolOutcome(
                status=ToolStatus.QUOTA_EXCEEDED,
                error_code="daily_tool_call_cap",
                limitations=("daily_analytics_call_cap_reached",),
                extra=dict(QUOTA_UNITS_UNKNOWN),
            )
        return _outcome(await call(tool_input))

    return execute


def build(
    provider: VideoInsightProvider, budget: CallBudget, *, daily_cap: int
) -> Mapping[ToolId, ToolExecutor]:
    if daily_cap < 1:
        raise ValueError("daily_cap must be >= 1")

    def make[M: BaseModel](
        model: type[M], call: Callable[[M], Awaitable[InsightResult[Any]]]
    ) -> ToolExecutor:
        return _executor(model, budget, daily_cap, call)

    return {
        ToolId.GET_VIDEO_ANALYTICS: make(
            VideoAnalyticsInput,
            lambda i: provider.video_analytics(i.video_id, i.start_date, i.end_date, i.metrics),
        ),
        ToolId.GET_AUDIENCE_RETENTION: make(
            RetentionInput,
            lambda i: provider.audience_retention(i.video_id, i.start_date, i.end_date),
        ),
        ToolId.COMPARE_VIDEO_PERFORMANCE: make(
            CompareInput, lambda i: provider.compare_videos(i.video_ids, i.days)
        ),
        ToolId.GET_AUDIENCE_DEMOGRAPHICS: make(
            DemographicsInput,
            lambda i: provider.audience_demographics(i.video_id, i.start_date, i.end_date),
        ),
        ToolId.GET_CHANNEL_PERFORMANCE: make(
            ChannelInput, lambda i: provider.channel_performance(i.start_date, i.end_date)
        ),
    }
