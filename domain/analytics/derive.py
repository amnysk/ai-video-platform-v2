"""Analytics の導出（純粋関数）。API の値ではなく**このシステムが計算した値**を作る。

呼び出し側は結果を ``derived`` と明示すること（ADR-0047 §5）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from contracts.agent_tools_analytics import (
    SHORTS_VIEWS_REDEFINITION_UNTIL,
    RetentionDrop,
)


def largest_drops(
    points: Sequence[tuple[float, float | None]], *, k: int
) -> tuple[RetentionDrop, ...]:
    """連続する 2 点の落ち幅が大きい順に k 件。上昇・欠測を含む区間は無視。同値は前の位置が先。"""
    drops: list[RetentionDrop] = []
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        if y0 is None or y1 is None:
            continue
        delta = y1 - y0
        if delta < 0:
            drops.append(RetentionDrop(from_ratio=x0, to_ratio=x1, delta=delta))
    drops.sort(key=lambda d: (d.delta, d.from_ratio))
    return tuple(drops[:k])


def age_days(published: date, confirmed_end: date) -> int:
    """公開日（0 日目）から確定済みの最終日まで、データがありうる日数。"""
    return max(0, (confirmed_end - published).days + 1)


def aligned_window(published: date, n: int) -> tuple[date, date]:
    return published, published + timedelta(days=n - 1)


def shorts_views_affected(window_start: date) -> bool:
    """Shorts の ``views`` 定義変更（2025-03-31）の移行期間（〜2025-04-30）を含む窓か。"""
    return window_start <= SHORTS_VIEWS_REDEFINITION_UNTIL


def weighted_mean(pairs: Iterable[tuple[float, float]]) -> float | None:
    """(値, 重み) の加重平均。重みの合計が 0 なら None。"""
    total_w = 0.0
    acc = 0.0
    for value, weight in pairs:
        acc += value * weight
        total_w += weight
    return acc / total_w if total_w > 0 else None
