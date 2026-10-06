"""Trend の増加速度（ADR-0039 §1）。純粋関数のみ（壁時計・乱数・I/O を使わない。INV-6）。

- ``views_per_hour_delta``: 同じ動画の **2 時点以上の観測**の差分から期間内の伸びを出す。
  最も古い観測と最も新しい観測を使う。観測が 1 時点しか無い・時間差が 0 以下・
  再生数が減った・同じ時刻に食い違う値がある・入力が不正なら ``unknown``（**0 にしない**）
- ``lifetime_average_views_per_hour``: 累積再生数 ÷ 公開からの時間。
  **参考値**であって期間内の伸びではない。名前も method も別（``lifetime_average``）にして
  混同させない

戻り値は契約の ``MetricReading``（``known`` は値と ``observed_at``、``unknown`` は理由を持つ）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from contracts.research_trend import MetricReading, MetricStatus, StatMetric

__all__ = [
    "VIEWS_PER_HOUR_UNIT",
    "ViewObservation",
    "lifetime_average_views_per_hour",
    "views_per_hour_delta",
]

VIEWS_PER_HOUR_UNIT = "views/hour"
_SECONDS_PER_HOUR = 3600.0


@dataclass(frozen=True, slots=True)
class ViewObservation:
    """ある動画の、ある時点の累積再生数。``views=None`` は「非公開・取得不可」。"""

    views: int | None
    observed_at: datetime


def _unknown(reason: str) -> MetricReading:
    return MetricReading(status=MetricStatus.UNKNOWN, unknown_reason=reason)


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _valid_views(views: int | None) -> bool:
    return isinstance(views, int) and not isinstance(views, bool) and views >= 0


def views_per_hour_delta(observations: Sequence[ViewObservation]) -> MetricReading:
    """``(views_latest - views_earliest) / 時間差``。観測日時は最も新しい観測のもの。"""
    if not all(_aware(o.observed_at) for o in observations):
        return _unknown("observation time is not timezone-aware")
    if not all(_valid_views(o.views) for o in observations):
        return _unknown("view count is missing or invalid in one of the observations")
    by_time: dict[datetime, int] = {}
    for observation in observations:
        assert observation.views is not None
        seen = by_time.setdefault(observation.observed_at, observation.views)
        if seen != observation.views:
            return _unknown("conflicting view counts were observed at the same time")
    if len(by_time) < 2:
        return _unknown("a delta needs at least two observations of the same video")
    first_at, last_at = min(by_time), max(by_time)
    first, last = by_time[first_at], by_time[last_at]
    hours = (last_at - first_at).total_seconds() / _SECONDS_PER_HOUR
    if last < first:
        return _unknown("view count decreased between observations")
    return MetricReading(
        status=MetricStatus.KNOWN,
        value=(last - first) / hours,
        unit=VIEWS_PER_HOUR_UNIT,
        metric=StatMetric.VIEWS_PER_HOUR_DELTA,
        observed_at=last_at,
    )


def lifetime_average_views_per_hour(
    views: int | None, published_at: datetime, observed_at: datetime
) -> MetricReading:
    """``views / (observed_at - published_at の時間)``。参考値（期間内の伸びではない）。"""
    if not (_aware(published_at) and _aware(observed_at)):
        return _unknown("time is not timezone-aware")
    if not _valid_views(views):
        return _unknown("view count is missing or invalid")
    assert views is not None
    hours = (observed_at - published_at).total_seconds() / _SECONDS_PER_HOUR
    if hours <= 0:
        return _unknown("observed_at is not later than published_at")
    return MetricReading(
        status=MetricStatus.KNOWN,
        value=views / hours,
        unit=VIEWS_PER_HOUR_UNIT,
        metric=StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR,
        observed_at=observed_at,
    )
