"""Topic Planner の外部ポート（ADR-0025）。

Analytics の実装（YouTube Analytics API）は ``infrastructure/analytics``。
ここは HTTP も OAuth も知らない。
取得に失敗したら ``domain.errors.AnalyticsUnavailableError`` を送出する。
"""

from __future__ import annotations

from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True, slots=True)
class VideoMetrics:
    """1 動画・1 窓の指標。"""

    video_id: str
    views: float
    estimated_minutes_watched: float
    average_view_duration: float
    average_view_percentage: float
    likes: float
    shares: float
    subscribers_gained: float
    #: 追加（既定 0.0）。古い snapshot には無い
    subscribers_lost: float = 0.0


@dataclass(frozen=True, slots=True)
class AudienceShares:
    """チャンネル視聴者の構成比（0..1）。取れなかった項目は None。"""

    country_us: float | None = None
    age_18_24: float | None = None
    age_25_34: float | None = None
    shorts: float | None = None
    #: 以下は追加（best-effort）。鍵は API の値（``age18-24``・``female``・``US``・``SHORTS`` 等）
    age_groups: dict[str, float] | None = None
    genders: dict[str, float] | None = None
    countries: dict[str, float] | None = None
    content_types: dict[str, float] | None = None


@dataclass(frozen=True, slots=True)
class AnalyticsReport:
    """窓（日数）ごとの動画別指標とチャンネルの視聴者構成。"""

    videos_by_window: dict[int, list[VideoMetrics]] = field(default_factory=dict)
    audience: AudienceShares = field(default_factory=AudienceShares)


class AnalyticsProvider(Protocol):
    async def fetch(self, window_days: AbstractSet[int]) -> AnalyticsReport:
        """窓ごとの指標を取る。失敗は ``AnalyticsUnavailableError``。"""
        ...
