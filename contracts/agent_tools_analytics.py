"""YouTube Analytics 系 Agent Tool の入出力と ToolDef（ADR-0047 §5）。

- 全 tool が read-only・``cost_class=youtube_quota``。
- derived を名乗る値は API の値ではなく、このシステムが決定論的に計算した値（根拠は limitations）。
- 取得できない値は ``None``（0 で埋めない / INV-58）。
- 日付は YouTube Analytics の日（UTC 暦日として扱う。太平洋時間との差は ``limitations`` に残す）。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.agent_tools import ToolDef, ToolId

VIDEO_ID_PATTERN = r"^[A-Za-z0-9_-]{11}$"

#: ``views`` の定義が Shorts で変わった日（2025-03-31）と、その移行が終わる日（2025-04-30）
SHORTS_VIEWS_REDEFINED_ON = date(2025, 3, 31)
SHORTS_VIEWS_REDEFINITION_UNTIL = date(2025, 4, 30)

#: 公式 API の channel report に無い指標（Studio 専用）。要求されたら ``not_available_via_api``
UNAVAILABLE_METRICS = ("impressions", "impressionsClickThroughRate")

VideoMetricName = Literal[
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "likes",
    "comments",
    "subscribersGained",
    "impressions",
    "impressionsClickThroughRate",
]

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class _Range(BaseModel):
    model_config = _FROZEN
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _ordered(self) -> _Range:
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must be <= end_date")
        return self


# --- get_video_analytics ---------------------------------------------------------------


class VideoAnalyticsInput(_Range):
    video_id: str = Field(pattern=VIDEO_ID_PATTERN)
    #: 省略は全部。``impressions`` 系を含めると ``not_available_via_api``
    metrics: tuple[VideoMetricName, ...] | None = Field(default=None, min_length=1, max_length=9)


class VideoAnalyticsOutput(BaseModel):
    model_config = _FROZEN
    video_id: str
    start_date: date
    end_date: date
    published_at: str | None = None
    views: float | None = None
    engaged_views: float | None = None
    estimated_minutes_watched: float | None = None
    average_view_duration_seconds: float | None = None
    likes: float | None = None
    comments: float | None = None
    subscribers_gained: float | None = None


# --- get_audience_retention ---------------------------------------------------------------


class RetentionInput(_Range):
    video_id: str = Field(pattern=VIDEO_ID_PATTERN)


class RetentionPoint(BaseModel):
    model_config = _FROZEN
    #: elapsedVideoTimeRatio（0..1。動画の相対位置）
    position_ratio: float
    audience_watch_ratio: float | None
    relative_retention_performance: float | None


class RetentionDrop(BaseModel):
    """連続する 2 点の audience_watch_ratio の差（derived）。"""

    model_config = _FROZEN
    from_ratio: float
    to_ratio: float
    #: ``to - from``（負 = 離脱）
    delta: float


class RetentionOutput(BaseModel):
    model_config = _FROZEN
    video_id: str
    start_date: date
    end_date: date
    points: tuple[RetentionPoint, ...]
    #: derived: 連続 2 点の落ち幅の大きい順（最大 3 件）
    largest_drops: tuple[RetentionDrop, ...]
    derived_fields: tuple[str, ...] = ("largest_drops",)


# --- compare_video_performance ---------------------------------------------------------------


class CompareInput(BaseModel):
    model_config = _FROZEN
    video_ids: tuple[str, ...] = Field(min_length=2, max_length=10)
    #: 公開後の比較日数。省略は全動画の最小経過日数
    days: int | None = Field(default=None, ge=1, le=90)

    @model_validator(mode="after")
    def _ids(self) -> CompareInput:
        import re

        if len(set(self.video_ids)) != len(self.video_ids):
            raise ValueError("video_ids must be unique")
        if any(not re.match(VIDEO_ID_PATTERN, v) for v in self.video_ids):
            raise ValueError("video_ids must be 11-character YouTube ids")
        return self


class CompareVideo(BaseModel):
    model_config = _FROZEN
    video_id: str
    published_at: str
    duration_seconds: float | None
    #: 公開日 0 日目から ``window_days`` 日分の合計 / 平均
    views: float | None
    engaged_views: float
    estimated_minutes_watched: float
    #: derived: 日別の average を views で重み付けした平均
    average_view_duration_seconds: float | None
    #: derived: 同上（動画尺で正規化された値。単位は API の percent）
    average_view_percentage: float | None


class CompareGroup(BaseModel):
    model_config = _FROZEN
    #: creatorContentType（SHORTS / VIDEO_ON_DEMAND 等）。**グループ間で順位を付けない**
    content_type: str
    status: Literal["ok", "insufficient_data"]
    reason: str | None = None
    window_days: int
    #: ``views`` の状態。Shorts で 2025-04-30 以前を含む窓は insufficient_data
    views_status: Literal["ok", "insufficient_data"]
    videos: tuple[CompareVideo, ...]
    #: derived: 指標ごとの順位（グループ内のみ。大きい順の video_id）
    ranking_within_group: dict[str, tuple[str, ...]] = Field(default_factory=dict)


class CompareOutput(BaseModel):
    model_config = _FROZEN
    window_days: int
    groups: tuple[CompareGroup, ...]
    excluded: dict[str, str] = Field(default_factory=dict)


# --- get_audience_demographics ---------------------------------------------------------------


class DemographicsInput(_Range):
    video_id: str | None = Field(default=None, pattern=VIDEO_ID_PATTERN)


class AgeGenderShare(BaseModel):
    model_config = _FROZEN
    age_group: str
    gender: str
    #: API の viewerPercentage（次元間で合算しない）
    viewer_percentage: float


class CountryShare(BaseModel):
    model_config = _FROZEN
    country: str
    #: derived: country 別 views / 全 country の views（0..1）。viewerPercentage ではない
    share_of_views: float


class DemographicsOutput(BaseModel):
    model_config = _FROZEN
    video_id: str | None
    start_date: date
    end_date: date
    #: 行が無い（閾値で隠れた可能性）なら None。0 ではない
    age_gender: tuple[AgeGenderShare, ...] | None
    countries: tuple[CountryShare, ...] | None


# --- get_channel_performance ---------------------------------------------------------------


class ChannelInput(_Range):
    pass


class FormatTotals(BaseModel):
    model_config = _FROZEN
    content_type: str
    views: float
    engaged_views: float
    estimated_minutes_watched: float
    subscribers_gained: float
    subscribers_lost: float


class DailySubscribers(BaseModel):
    model_config = _FROZEN
    date: date
    subscribers_gained: float
    subscribers_lost: float


class ChannelOutput(BaseModel):
    model_config = _FROZEN
    start_date: date
    end_date: date
    views: float | None
    engaged_views: float | None
    estimated_minutes_watched: float | None
    average_view_duration_seconds: float | None
    subscribers_gained: float | None
    subscribers_lost: float | None
    by_format: tuple[FormatTotals, ...]
    daily_subscribers: tuple[DailySubscribers, ...]
    #: 期間内に公開した動画数（形式別には分けられない）。数えきれなければ None
    published_count: int | None
    published_count_truncated: bool = False


def _def(
    tool_id: ToolId,
    description: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    *,
    timeout_seconds: int,
    max_calls_per_run: int,
) -> ToolDef:
    return ToolDef(
        tool_id=tool_id,
        version=1,
        description=description,
        input_model=input_model,
        output_model=output_model,
        timeout_seconds=timeout_seconds,
        max_calls_per_run=max_calls_per_run,
        cost_class="youtube_quota",
    )


ANALYTICS_TOOL_DEFS: tuple[ToolDef, ...] = (
    _def(
        ToolId.GET_VIDEO_ANALYTICS,
        "Read one video's views, engagedViews, watch time, average view duration, likes, comments "
        "and subscribers gained for a date range (default: publish date to last confirmed day).",
        VideoAnalyticsInput,
        VideoAnalyticsOutput,
        timeout_seconds=30,
        max_calls_per_run=5,
    ),
    _def(
        ToolId.GET_AUDIENCE_RETENTION,
        "Read one video's audience retention curve (relative position) plus a derived "
        "drop-off summary.",
        RetentionInput,
        RetentionOutput,
        timeout_seconds=30,
        max_calls_per_run=3,
    ),
    _def(
        ToolId.COMPARE_VIDEO_PERFORMANCE,
        "Compare 2-10 videos aligned by days since publish, per format group "
        "(Shorts are never ranked against long-form).",
        CompareInput,
        CompareOutput,
        timeout_seconds=90,
        max_calls_per_run=2,
    ),
    _def(
        ToolId.GET_AUDIENCE_DEMOGRAPHICS,
        "Read age/gender viewerPercentage and country view shares for the channel or one video. "
        "Rows hidden by privacy thresholds are reported as withheld, never as 0.",
        DemographicsInput,
        DemographicsOutput,
        timeout_seconds=30,
        max_calls_per_run=3,
    ),
    _def(
        ToolId.GET_CHANNEL_PERFORMANCE,
        "Read channel totals, daily subscriber trend, per-format totals and published video count "
        "for a date range.",
        ChannelInput,
        ChannelOutput,
        timeout_seconds=60,
        max_calls_per_run=2,
    ),
)
