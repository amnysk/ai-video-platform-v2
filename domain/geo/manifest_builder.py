"""依頼（座標なし）から GeoManifest を決定論的に作る（ADR-0084 §2、INV-139）。純関数。

座標・地物の種類・存在期間・位置の不確かさは **gazetteer からだけ**取る。依頼の作者（LLM / 人）は
地点を ``ref_id`` で指すだけで、座標を書けない。根拠（claim / source）が無い地点、gazetteer に
無い地点、``ref_id`` が曖昧な地点は、manifest を作らず BLOCK の finding にする（推測で埋めない）。
時代の整合（その名前が ``as_of`` に使われていたか）はここでは見ない。検証器が見る。
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError

from contracts.geo import (
    GeoLabel,
    GeoManifest,
    GeoPlace,
    GeoVerdict,
    MapKind,
    ReferencePlace,
)
from contracts.geo_map import MapFinding, MapReasonCode, MapTargetKind
from contracts.geo_scene import GeoMapRequest, GeoPlaceRequest


def _block(code: MapReasonCode, target_kind: MapTargetKind, target_id: str) -> MapFinding:
    return MapFinding(
        verdict=GeoVerdict.BLOCK, reason_code=code, target_kind=target_kind, target_id=target_id
    )


def _place(req: GeoPlaceRequest, ref: ReferencePlace) -> GeoPlace:
    return GeoPlace(
        place_id=ref.ref_id,
        display_name=req.display_name,
        lat=ref.lat,
        lon=ref.lon,
        crs="EPSG:4326",
        feature_type=ref.feature_type,
        location_confidence=req.location_confidence,
        uncertainty_radius_km=ref.uncertainty_radius_km,
        valid_start_year=ref.valid_start_year,
        valid_end_year=ref.valid_end_year,
        source_ids=req.source_ids,
        claim_id=req.claim_id,
    )


def build_manifest(
    request: GeoMapRequest, gazetteer: Sequence[ReferencePlace]
) -> tuple[GeoManifest | None, tuple[MapFinding, ...]]:
    """manifest と、作れなかった理由。findings があれば manifest は ``None``。"""
    findings: list[MapFinding] = []
    places: list[GeoPlace] = []
    labels: list[GeoLabel] = []
    real = request.map_kind is not MapKind.SCHEMATIC
    seen: set[str] = set()
    for req in sorted(request.places, key=lambda p: p.ref_id):
        if req.ref_id in seen:
            findings.append(_block(MapReasonCode.AMBIGUOUS_PLACE, MapTargetKind.PLACE, req.ref_id))
            continue
        seen.add(req.ref_id)
        matches = [r for r in gazetteer if r.ref_id == req.ref_id]
        if not matches:
            findings.append(_block(MapReasonCode.UNKNOWN_PLACE, MapTargetKind.PLACE, req.ref_id))
            continue
        if len(matches) > 1:
            findings.append(_block(MapReasonCode.AMBIGUOUS_PLACE, MapTargetKind.PLACE, req.ref_id))
            continue
        if real and req.claim_id is None:
            findings.append(_block(MapReasonCode.EVIDENCE_MISSING, MapTargetKind.PLACE, req.ref_id))
            continue
        if real and not req.source_ids:
            findings.append(_block(MapReasonCode.SOURCE_MISSING, MapTargetKind.PLACE, req.ref_id))
            continue
        try:
            places.append(_place(req, matches[0]))
            labels.append(GeoLabel(place_id=req.ref_id, text=req.label or req.display_name))
        except ValidationError:
            findings.append(_block(MapReasonCode.UNCLASSIFIED, MapTargetKind.PLACE, req.ref_id))
    if findings:
        return None, tuple(findings)
    try:
        manifest = GeoManifest(
            scene_id=request.scene_id,
            map_kind=request.map_kind,
            as_of=request.as_of,
            bbox=request.bbox,
            places=tuple(places),
            labels=tuple(labels),
            spatial_assertions=request.spatial_assertions,
            borders=request.borders,
            border_valid_year=request.border_valid_year,
            schematic_disclaimer=request.schematic_disclaimer,
        )
    except ValidationError:
        return None, (_block(MapReasonCode.UNCLASSIFIED, MapTargetKind.MANIFEST, request.scene_id),)
    return manifest, ()


__all__ = ["build_manifest"]
