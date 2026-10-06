"""Trend の純粋な規則: 増加速度と鮮度（ADR-0039 §1 / §2）。

- 差分の増加速度（``views_per_hour_delta``）は**同じ動画の 2 時点以上**の観測があるときだけ。
  1 時点しか無ければ ``unknown``（0 にしない）。累積 ÷ 公開からの時間は別の名前
  （``lifetime_average``）の参考値
- 鮮度は ``fresh`` / ``stale``（日時つき）/ ``none`` の 3 値で、窓は設定値
  ``trend_fresh_hours`` を引数で受ける。未来の観測（時計ずれ）は採らない

理由は docs/testing/research-trend-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.research import TREND_FRESH_HOURS, TREND_STALE_MAX_DAYS
from contracts.research_trend import MetricStatus, StatMetric
from domain.research.trend_freshness import TrendFreshness, classify_trend_freshness
from domain.research.trend_metrics import (
    ViewObservation,
    lifetime_average_views_per_hour,
    views_per_hour_delta,
)

T1 = datetime(2026, 8, 29, 0, 0, tzinfo=UTC)
T2 = T1 + timedelta(hours=10)


# ------------------------------------------------------------------ 増加速度


def test_the_delta_needs_at_least_two_observations_of_the_same_video() -> None:
    one = views_per_hour_delta([ViewObservation(1000, T1)])
    assert one.status is MetricStatus.UNKNOWN and one.value is None
    assert "two observations" in (one.unknown_reason or "")
    assert views_per_hour_delta([]).status is MetricStatus.UNKNOWN
    same_time = views_per_hour_delta([ViewObservation(1000, T1), ViewObservation(1000, T1)])
    assert same_time.status is MetricStatus.UNKNOWN


def test_the_delta_uses_the_earliest_and_latest_observation() -> None:
    reading = views_per_hour_delta(
        [
            ViewObservation(1500, T1 + timedelta(hours=5)),
            ViewObservation(2000, T2),
            ViewObservation(1000, T1),
        ]
    )
    assert reading.status is MetricStatus.KNOWN
    assert reading.metric is StatMetric.VIEWS_PER_HOUR_DELTA
    assert reading.value == pytest.approx(100.0)
    assert reading.observed_at == T2


@pytest.mark.parametrize(
    ("observations", "reason"),
    [
        ([ViewObservation(2000, T1), ViewObservation(1000, T2)], "decreased"),
        ([ViewObservation(None, T1), ViewObservation(1000, T2)], "missing"),
        ([ViewObservation(1000, T1), ViewObservation(1200, T1)], "conflicting"),
        ([ViewObservation(1000, T1.replace(tzinfo=None)), ViewObservation(1, T2)], "timezone"),
    ],
)
def test_an_unusable_delta_is_unknown_with_a_reason_never_zero(observations, reason) -> None:
    reading = views_per_hour_delta(observations)
    assert reading.status is MetricStatus.UNKNOWN and reading.value is None
    assert reason in (reading.unknown_reason or "")


def test_the_lifetime_average_is_a_separately_named_reference_value() -> None:
    published = T1 - timedelta(hours=100)
    reading = lifetime_average_views_per_hour(5000, published, T1)
    assert reading.status is MetricStatus.KNOWN
    assert reading.metric is StatMetric.LIFETIME_AVERAGE_VIEWS_PER_HOUR
    assert reading.value == pytest.approx(50.0)
    assert lifetime_average_views_per_hour(None, published, T1).status is MetricStatus.UNKNOWN
    assert lifetime_average_views_per_hour(5000, T1, T1).status is MetricStatus.UNKNOWN


# ------------------------------------------------------------------ 鮮度


NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def test_freshness_is_fresh_within_the_configured_window() -> None:
    verdict = classify_trend_freshness(NOW - timedelta(hours=3), NOW, fresh_hours=24)
    assert verdict.mode is TrendFreshness.FRESH
    assert verdict.observed_at == NOW - timedelta(hours=3)
    assert verdict.age == timedelta(hours=3)
    # 窓は設定値（引数）で決まる。既定値の再宣言は無い
    assert (
        classify_trend_freshness(NOW - timedelta(hours=3), NOW, fresh_hours=2).mode
        is TrendFreshness.STALE
    )


def test_a_stale_trend_carries_its_date_until_the_stale_limit() -> None:
    observed = NOW - timedelta(days=3)
    verdict = classify_trend_freshness(observed, NOW, fresh_hours=TREND_FRESH_HOURS)
    assert verdict.mode is TrendFreshness.STALE
    assert verdict.observed_at == observed
    too_old = NOW - timedelta(days=TREND_STALE_MAX_DAYS, seconds=1)
    assert classify_trend_freshness(too_old, NOW, fresh_hours=24).mode is TrendFreshness.NONE


def test_no_trend_or_a_future_observation_is_none() -> None:
    none = classify_trend_freshness(None, NOW, fresh_hours=24)
    assert (none.mode, none.observed_at, none.age) == (TrendFreshness.NONE, None, None)
    future = classify_trend_freshness(NOW + timedelta(minutes=1), NOW, fresh_hours=24)
    assert future.mode is TrendFreshness.NONE


def test_freshness_rejects_naive_times_and_a_non_positive_window() -> None:
    with pytest.raises(ValueError):
        classify_trend_freshness(NOW.replace(tzinfo=None), NOW, fresh_hours=24)
    with pytest.raises(ValueError):
        classify_trend_freshness(NOW, NOW.replace(tzinfo=None), fresh_hours=24)
    with pytest.raises(ValueError):
        classify_trend_freshness(NOW, NOW, fresh_hours=0)
