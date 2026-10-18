"""動画別 insight の port（ADR-0047 §5）。実装は ``infrastructure/analytics``。

- 全メソッド read-only。失敗は例外ではなく ``InsightResult.status``（``ToolStatus`` の意味）で返す。
- 想定外の障害（バグ）だけ例外にしてよい。
- ``quota_units`` は公式に単価が見つからないので常に ``"unknown"``（0 にしない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from pydantic import BaseModel

from contracts.agent_tools import ToolStatus
from contracts.agent_tools_analytics import (
    ChannelOutput,
    CompareOutput,
    DemographicsOutput,
    RetentionOutput,
    VideoAnalyticsOutput,
)


@dataclass(frozen=True, slots=True)
class InsightResult[T: BaseModel]:
    status: ToolStatus
    #: ``status`` が ok のときだけ持つ
    data: T | None = None
    #: 機械可読コード（例 ``shorts_views_definition_change``）
    limitations: tuple[str, ...] = ()
    #: ``analytics_query:<sha256>``
    evidence_refs: tuple[str, ...] = ()
    retrieved_at: str | None = None
    #: 診断用の短いコード。token・URL の query・応答本文を入れない（INV-20）
    error_code: str | None = None
    quota_units: str = "unknown"


class VideoInsightProvider(Protocol):
    async def video_analytics(
        self,
        video_id: str,
        start: date | None,
        end: date | None,
        metrics: tuple[str, ...] | None,
    ) -> InsightResult[VideoAnalyticsOutput]: ...

    async def audience_retention(
        self, video_id: str, start: date | None, end: date | None
    ) -> InsightResult[RetentionOutput]: ...

    async def compare_videos(
        self, video_ids: tuple[str, ...], days: int | None
    ) -> InsightResult[CompareOutput]: ...

    async def audience_demographics(
        self, video_id: str | None, start: date | None, end: date | None
    ) -> InsightResult[DemographicsOutput]: ...

    async def channel_performance(
        self, start: date | None, end: date | None
    ) -> InsightResult[ChannelOutput]: ...


class CallBudget(Protocol):
    """Analytics tool の日次呼び出し回数。DB 実装は別担当。"""

    async def try_consume(self, day: date, limit: int) -> bool:
        """``day`` の回数が ``limit`` 未満なら 1 増やして True。上限なら増やさず False。"""
        ...
