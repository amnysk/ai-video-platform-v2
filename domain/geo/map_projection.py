"""地図の投影（ADR-0078、INV-119）。純関数（INV-6）。**投影の定義はここだけ**。

等緩投影（equirectangular）を bbox の中心緯度の cos で経度方向に縮めて縦横比を保ち、
margin を引いた領域の中央に収める。結果は整数 px（四捨五入は ``round``＝偶数丸めに依らないよう
``floor(x + 0.5)``）。乱数・時刻なし。
"""

from __future__ import annotations

import math

from contracts.geo import GeoBBox


def _round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def project_point(
    bbox: GeoBBox, width: int, height: int, margin: int, lat: float, lon: float
) -> tuple[int, int]:
    """(lat, lon) → (x, y)。x は東へ増え、y は北へ減る（左上原点）。"""
    inner_w = width - 2 * margin
    inner_h = height - 2 * margin
    if inner_w <= 0 or inner_h <= 0:
        raise ValueError("margin leaves no drawing area")
    mid_lat = (bbox.min_lat + bbox.max_lat) / 2.0
    k = math.cos(math.radians(mid_lat))
    span_x = (bbox.max_lon - bbox.min_lon) * k
    span_y = bbox.max_lat - bbox.min_lat
    scale = min(inner_w / span_x, inner_h / span_y)
    off_x = margin + (inner_w - span_x * scale) / 2.0
    off_y = margin + (inner_h - span_y * scale) / 2.0
    x = off_x + (lon - bbox.min_lon) * k * scale
    y = off_y + (bbox.max_lat - lat) * scale
    return _round_half_up(x), _round_half_up(y)


__all__ = ["project_point"]
