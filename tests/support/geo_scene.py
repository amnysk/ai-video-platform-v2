"""Phase 3 geo の fixture（ADR-0084）。メモリ上の固定データだけ。ネットワーク・ファイルに出ない。

座標は**テスト用の概略値**（実在の地点の市街・遺構の代表点。数 km の誤差を持つ）で、
本番の gazetteer の代わりにならない。本番のデータはオーナーが供給する（ADR-0084 §9）。
"""

from __future__ import annotations

from typing import Any

from contracts.geo import (
    GeoBBox,
    GeoFeatureType,
    GeoManifest,
    LocationConfidence,
    MapKind,
    ReferenceName,
    ReferencePlace,
)
from contracts.geo_scene import GeoMapRequest, GeoMapSpec, GeoPlaceRequest
from contracts.research import ClaimImportance, ClaimKind
from contracts.research_evidence import (
    ClaimAssessment,
    EvidenceClaim,
    EvidenceSource,
    SourceFetchStatus,
    SourceKind,
)

YASHIMA = ReferencePlace(
    ref_id="gz:yashima",
    names=(ReferenceName(name="Yashima"), ReferenceName(name="屋島")),
    lat=34.36,
    lon=134.11,
    feature_type=GeoFeatureType.NATURAL,
    uncertainty_radius_km=3.0,
)
KYOTO = ReferencePlace(
    ref_id="gz:kyoto",
    names=(
        ReferenceName(name="Heian-kyo", start_year=794, end_year=1868),
        ReferenceName(name="Kyoto"),
        ReferenceName(name="京都"),
    ),
    lat=35.0116,
    lon=135.7681,
    feature_type=GeoFeatureType.SETTLEMENT,
    uncertainty_radius_km=2.0,
)
ICHINOTANI = ReferencePlace(
    ref_id="gz:ichinotani",
    names=(ReferenceName(name="Ichi-no-Tani"), ReferenceName(name="一ノ谷")),
    lat=34.655,
    lon=135.145,
    feature_type=GeoFeatureType.BATTLEFIELD,
    uncertainty_radius_km=3.0,
)
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
GAZETTEER: tuple[ReferencePlace, ...] = (YASHIMA, KYOTO, ICHINOTANI, EDO_TOKYO)

#: portrait 地図（1080x1920）で Shikoku 東部〜近畿を収める想定地域
KINKI_SHIKOKU_BBOX = GeoBBox(min_lon=132.6, min_lat=33.2, max_lon=136.6, max_lat=36.0)

CLAIM_TEXT = "Locations of Yashima, Heian-kyo, Kyoto, Ichi-no-Tani, Edo and Tokyo in 1185"


def claim(claim_id: str = "C-001", assessment: ClaimAssessment = ClaimAssessment.SUPPORTED) -> Any:
    insufficient = assessment is ClaimAssessment.INSUFFICIENT
    return EvidenceClaim(
        claim_id=claim_id,
        text=CLAIM_TEXT,
        kind=ClaimKind.PLACE,
        importance=ClaimImportance.CENTRAL,
        strong=False,
        assessed=not insufficient,
        assessment=assessment,
        assessment_reason="fixture",
        usable_expression="" if insufficient else "fixture expression",
    )


def source(source_id: str = "S-001", status: SourceFetchStatus = SourceFetchStatus.FETCHED) -> Any:
    fetched = status is SourceFetchStatus.FETCHED
    return EvidenceSource(
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        title="fixture source",
        language="en",
        retrieved_at="2026-10-01T00:00:00+00:00",  # type: ignore[arg-type]
        content_sha256="a" * 64 if fetched else None,
        fetch_status=status,
        origin_key=f"origin-{source_id}",
        source_kind=SourceKind.REFERENCE,
    )


CLAIMS = (claim("C-001"),)
SOURCES = (source("S-001"),)


def place_req(ref_id: str = "gz:yashima", name: str = "Yashima", **over: Any) -> GeoPlaceRequest:
    base: dict[str, Any] = {
        "ref_id": ref_id,
        "display_name": name,
        "claim_id": "C-001",
        "source_ids": ("S-001",),
        "location_confidence": LocationConfidence.HIGH,
    }
    base.update(over)
    return GeoPlaceRequest.model_validate(base)


def request(**over: Any) -> GeoMapRequest:
    base: dict[str, Any] = {
        "scene_id": "s2",
        "map_kind": MapKind.HISTORICAL,
        "as_of": "1185-03",
        "bbox": KINKI_SHIKOKU_BBOX,
        "places": (
            place_req("gz:yashima", "Yashima"),
            place_req("gz:kyoto", "Heian-kyo"),
            place_req("gz:ichinotani", "Ichi-no-Tani"),
        ),
    }
    base.update(over)
    return GeoMapRequest.model_validate(base)


def spec(**over: Any) -> GeoMapSpec:
    return GeoMapSpec(storyboard_scene_id="sb3", request=request(**over))


def manifest_from(spec_: GeoMapSpec) -> GeoManifest:
    from domain.geo.manifest_builder import build_manifest

    assert spec_.request is not None
    manifest, findings = build_manifest(spec_.request, GAZETTEER)
    assert manifest is not None, findings
    return manifest
