"""距離の許容と段階（ADR-0071）。**この係数は 1 か所だけ**（初期値。実データで調整する）。

一律の km 閾値を使わない。許容 =（地物の種類ごとの基準 + 資料の不確かさ + 参照の不確かさ）
× 信頼度の倍率。
許容内は PASS、1.5 倍まで WARN、3 倍まで REVIEW、それを超えたら BLOCK。
"""

from __future__ import annotations

import math

from contracts.geo import GeoFeatureType, GeoVerdict, LocationConfidence

EARTH_RADIUS_KM = 6371.0088

BASE_TOLERANCE_KM: dict[GeoFeatureType, float] = {
    GeoFeatureType.STRUCTURE: 3.0,
    GeoFeatureType.SETTLEMENT: 10.0,
    GeoFeatureType.BATTLEFIELD: 15.0,
    GeoFeatureType.NATURAL: 20.0,
    GeoFeatureType.REGION: 100.0,
}
CONFIDENCE_MULTIPLIER: dict[LocationConfidence, float] = {
    LocationConfidence.HIGH: 1.0,
    LocationConfidence.MEDIUM: 2.0,
    LocationConfidence.LOW: 4.0,
}
WARN_FACTOR = 1.5
BLOCK_FACTOR = 3.0


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """球面上の距離（km）。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(a)))


def tolerance_km(
    feature_type: GeoFeatureType,
    confidence: LocationConfidence,
    place_uncertainty_km: float | None,
    reference_uncertainty_km: float,
) -> float:
    spread = BASE_TOLERANCE_KM[feature_type] + (place_uncertainty_km or 0.0)
    spread += reference_uncertainty_km
    return spread * CONFIDENCE_MULTIPLIER[confidence]


def classify_distance(distance_km: float, tolerance: float) -> GeoVerdict:
    if distance_km <= tolerance:
        return GeoVerdict.PASS
    if distance_km <= tolerance * WARN_FACTOR:
        return GeoVerdict.WARN
    if distance_km <= tolerance * BLOCK_FACTOR:
        return GeoVerdict.REVIEW
    return GeoVerdict.BLOCK
