"""検証器へ注入する fake gazetteer と、地図の fixture の部品（ADR-0071）。

メモリ上の固定データだけ。ネットワーク・ファイルに出ない。座標は実在地点のおおよその値で、
テストの意図（距離・軸順・同名異所・旧称）に必要な精度しか持たない。
"""

from __future__ import annotations

from typing import Any

from contracts.geo import GeoFeatureType, GeoManifest, ReferenceName, ReferencePlace
from contracts.research import ClaimImportance, ClaimKind
from contracts.research_evidence import ClaimAssessment, EvidenceClaim

SEKIGAHARA = ReferencePlace(
    ref_id="gz:sekigahara",
    names=(ReferenceName(name="Sekigahara"), ReferenceName(name="関ヶ原")),
    lat=35.3667,
    lon=136.4667,
    feature_type=GeoFeatureType.BATTLEFIELD,
    uncertainty_radius_km=3.0,
    valid_start_year=1000,
)
OSAKA_CASTLE = ReferencePlace(
    ref_id="gz:osaka-castle",
    names=(ReferenceName(name="Osaka Castle"), ReferenceName(name="大坂城", end_year=1868)),
    lat=34.6873,
    lon=135.5262,
    feature_type=GeoFeatureType.STRUCTURE,
    valid_start_year=1583,
)
#: 江戸 = 現在の東京。旧称と現称は同じ座標で、名前の有効期間だけが違う。
EDO_TOKYO = ReferencePlace(
    ref_id="gz:edo-tokyo",
    names=(
        ReferenceName(name="Edo", start_year=1457, end_year=1868),
        ReferenceName(name="Tokyo", start_year=1868),
    ),
    lat=35.6895,
    lon=139.6917,
    feature_type=GeoFeatureType.SETTLEMENT,
    uncertainty_radius_km=2.0,
)
KYOTO = ReferencePlace(
    ref_id="gz:kyoto",
    names=(ReferenceName(name="Kyoto"), ReferenceName(name="京都")),
    lat=35.0116,
    lon=135.7681,
    feature_type=GeoFeatureType.SETTLEMENT,
)
#: 同名異所（府中）。どちらを指すか manifest だけでは決まらない。
FUCHU_TOKYO = ReferencePlace(
    ref_id="gz:fuchu-tokyo",
    names=(ReferenceName(name="Fuchu"),),
    lat=35.6689,
    lon=139.4778,
    feature_type=GeoFeatureType.SETTLEMENT,
)
FUCHU_HIROSHIMA = ReferencePlace(
    ref_id="gz:fuchu-hiroshima",
    names=(ReferenceName(name="Fuchu"),),
    lat=34.5686,
    lon=133.2367,
    feature_type=GeoFeatureType.SETTLEMENT,
)
#: 軸順の取り違えが範囲内に収まる地点（緯度 41 / 経度 29 は入れ替えても |値| <= 90）。
CONSTANTINOPLE = ReferencePlace(
    ref_id="gz:constantinople",
    names=(
        ReferenceName(name="Constantinople", start_year=330, end_year=1930),
        ReferenceName(name="Istanbul", start_year=1930),
    ),
    lat=41.0082,
    lon=28.9784,
    feature_type=GeoFeatureType.SETTLEMENT,
)
#: 国のような広い地物（代表点が重心）。
JAPAN = ReferencePlace(
    ref_id="gz:honshu",
    names=(ReferenceName(name="Honshu"),),
    lat=36.0,
    lon=138.0,
    feature_type=GeoFeatureType.REGION,
)

ALL_REFERENCES: tuple[ReferencePlace, ...] = (
    SEKIGAHARA,
    OSAKA_CASTLE,
    EDO_TOKYO,
    KYOTO,
    FUCHU_TOKYO,
    FUCHU_HIROSHIMA,
    CONSTANTINOPLE,
    JAPAN,
)


def claim(
    claim_id: str = "C-001", assessment: ClaimAssessment = ClaimAssessment.SUPPORTED
) -> EvidenceClaim:
    """地点の主張。``supported`` / ``qualified`` だけが根拠として解決する。"""
    insufficient = assessment is ClaimAssessment.INSUFFICIENT
    return EvidenceClaim(
        claim_id=claim_id,
        text=f"place claim {claim_id}",
        kind=ClaimKind.PLACE,
        importance=ClaimImportance.CENTRAL,
        strong=False,
        assessed=not insufficient,
        assessment=assessment,
        assessment_reason="fixture",
        usable_expression="" if insufficient else "fixture expression",
    )


GOOD_CLAIMS: tuple[EvidenceClaim, ...] = (claim("C-001"), claim("C-002"))

JAPAN_BBOX = {"min_lon": 128.0, "min_lat": 30.0, "max_lon": 146.0, "max_lat": 46.0}
BYZANTINE_BBOX = {"min_lon": 20.0, "min_lat": 35.0, "max_lon": 45.0, "max_lat": 45.0}


def place(**over: Any) -> dict[str, Any]:
    """Sekigahara を正しい位置に置く place（既定）。``over`` で一部だけ壊す。"""
    base: dict[str, Any] = {
        "place_id": "gz:sekigahara",
        "display_name": "Sekigahara",
        "lat": 35.3667,
        "lon": 136.4667,
        "crs": "EPSG:4326",
        "feature_type": "battlefield",
        "location_confidence": "high",
        "uncertainty_radius_km": None,
        "valid_start_year": 1000,
        "valid_end_year": None,
        "source_ids": ["S-001"],
        "claim_id": "C-001",
    }
    base.update(over)
    return base


def manifest_payload(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "scene_id": "s1",
        "map_kind": "historical",
        "as_of": "1600-10",
        "bbox": JAPAN_BBOX,
        "places": [place()],
        "labels": [{"place_id": "gz:sekigahara", "text": "Sekigahara"}],
        "spatial_assertions": [],
    }
    base.update(over)
    return base


def manifest(**over: Any) -> GeoManifest:
    return GeoManifest.model_validate(manifest_payload(**over))
