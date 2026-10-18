"""domain/analytics/derive.py（純粋な導出）。理由は docs/testing/agent-tools-rationale.md。"""

from __future__ import annotations

from datetime import date

import pytest

from domain.analytics.derive import (
    age_days,
    aligned_window,
    largest_drops,
    shorts_views_affected,
    weighted_mean,
)


def test_largest_drops_orders_by_magnitude_and_ignores_rises() -> None:
    pts = [(0.0, 1.0), (0.1, 0.9), (0.2, 0.5), (0.3, 0.55), (0.4, 0.3)]
    drops = largest_drops(pts, k=2)
    assert [(d.from_ratio, d.to_ratio) for d in drops] == [(0.1, 0.2), (0.3, 0.4)]
    assert drops[0].delta == pytest.approx(-0.4)


def test_largest_drops_skips_missing_values_and_returns_empty_when_flat() -> None:
    assert largest_drops([(0.0, None), (0.1, 0.5), (0.2, None)], k=3) == ()
    assert largest_drops([(0.0, 0.5), (0.1, 0.5)], k=3) == ()


def test_largest_drops_tie_breaks_by_earlier_position() -> None:
    pts = [(0.0, 1.0), (0.1, 0.75), (0.2, 0.5)]
    assert [d.from_ratio for d in largest_drops(pts, k=2)] == [0.0, 0.1]


def test_age_days_counts_publish_day_through_confirmed_end() -> None:
    assert age_days(date(2026, 9, 1), date(2026, 9, 10)) == 10
    assert age_days(date(2026, 9, 11), date(2026, 9, 10)) == 0


def test_aligned_window_is_n_days_starting_on_publish_day() -> None:
    assert aligned_window(date(2026, 9, 1), 7) == (date(2026, 9, 1), date(2026, 9, 7))


@pytest.mark.parametrize(
    ("start", "affected"),
    [
        (date(2025, 3, 1), True),
        (date(2025, 4, 30), True),
        (date(2025, 5, 1), False),
        (date(2026, 9, 1), False),
    ],
)
def test_shorts_views_definition_window(start: date, affected: bool) -> None:
    assert shorts_views_affected(start) is affected


def test_weighted_mean_by_views_and_none_when_no_weight() -> None:
    assert weighted_mean([(10.0, 1.0), (40.0, 3.0)]) == pytest.approx(32.5)
    assert weighted_mean([(10.0, 0.0)]) is None
    assert weighted_mean([]) is None
