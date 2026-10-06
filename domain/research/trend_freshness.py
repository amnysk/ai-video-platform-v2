"""Trend の鮮度の判定（ADR-0039 §2）。純粋関数のみ（壁時計は引数 ``now`` で受ける。INV-6）。

読む側（B6 の Topic Planner）が、読み出した Trend の観測時刻
（``TrendArtifact.observed_at``）をこれで 3 値に分ける:

- ``fresh``: ``0 <= now - observed_at <= fresh_hours``
- ``stale``: その先 ``stale_max_days`` まで。**観測日時つき**で使う
  （古い Trend であることを隠さない）
- ``none``: Trend が無い・``stale_max_days`` より古い・**未来の観測**
  （時計ずれ。fresh とみなさない）。呼び出し側は「Trend 無し」として Trend 前の挙動で続ける

``fresh_hours`` は設定値 ``Settings.trend_fresh_hours`` を呼び出し側が渡す（既定値を再宣言しない。
既定は ``contracts/research.py``）。``stale_max_days`` の既定は ``TREND_STALE_MAX_DAYS``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from contracts.research import TREND_STALE_MAX_DAYS

__all__ = ["FreshnessVerdict", "TrendFreshness", "classify_trend_freshness"]


class TrendFreshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class FreshnessVerdict:
    mode: TrendFreshness
    #: 観測時刻（``none`` は ``None``）。``stale`` はこの日時を添えて使う
    observed_at: datetime | None
    #: ``now - observed_at``（``none`` は ``None``）
    age: timedelta | None


_NONE = FreshnessVerdict(mode=TrendFreshness.NONE, observed_at=None, age=None)


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def classify_trend_freshness(
    observed_at: datetime | None,
    now: datetime,
    *,
    fresh_hours: int,
    stale_max_days: int = TREND_STALE_MAX_DAYS,
) -> FreshnessVerdict:
    """観測時刻を ``fresh`` / ``stale`` / ``none`` に分ける。"""
    _require_aware(now, "now")
    if fresh_hours <= 0 or stale_max_days <= 0:
        raise ValueError("fresh_hours and stale_max_days must be positive")
    if observed_at is None:
        return _NONE
    _require_aware(observed_at, "observed_at")
    age = now - observed_at
    if age < timedelta(0):  # 時計ずれ。未来の観測は採らない
        return _NONE
    if age <= timedelta(hours=fresh_hours):
        return FreshnessVerdict(mode=TrendFreshness.FRESH, observed_at=observed_at, age=age)
    if age <= timedelta(days=stale_max_days):
        return FreshnessVerdict(mode=TrendFreshness.STALE, observed_at=observed_at, age=age)
    return _NONE
