"""GeoPlace / GeoManifest / GeoVerification の契約（ADR-0071、INV-95、INV-94 の契約側）。

理由は docs/testing/geo-contracts-rationale.md。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.geo import (
    GeoBBox,
    GeoCheck,
    GeoLabel,
    GeoManifest,
    GeoPlace,
    GeoVerdict,
    GeoVerification,
    stricter,
)


def _place(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "place_id": "sekigahara",
        "display_name": "Sekigahara",
        "lat": 35.3667,
        "lon": 136.4667,
        "crs": "EPSG:4326",
        "feature_type": "battlefield",
        "location_confidence": "high",
        "uncertainty_radius_km": None,
        "valid_start_year": 1500,
        "valid_end_year": 1700,
        "source_ids": ["S-001"],
        "claim_id": "C-001",
    }
    base.update(over)
    return base


def _manifest(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "scene_id": "s1",
        "map_kind": "historical",
        "as_of": "1600-10",
        "bbox": {"min_lon": 134.0, "min_lat": 34.0, "max_lon": 138.0, "max_lat": 36.5},
        "places": [_place()],
        "labels": [{"place_id": "sekigahara", "text": "Sekigahara"}],
        "spatial_assertions": [],
    }
    base.update(over)
    return base


def test_a_well_formed_manifest_round_trips() -> None:
    manifest = GeoManifest.model_validate(_manifest())
    assert GeoManifest.model_validate(manifest.model_dump(mode="json")) == manifest
    assert manifest.places[0].lat == 35.3667 and manifest.places[0].lon == 136.4667


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lat", 90.0001),
        ("lat", -91),
        ("lon", 180.5),
        ("lon", -181),
        ("lat", float("nan")),
        ("lon", float("inf")),
    ],
)
def test_coordinates_outside_epsg4326_ranges_are_rejected(field: str, value: float) -> None:
    """INV-95: 範囲外（日本の経度を緯度に入れた等）は契約で作れない。"""
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(**{field: value}))


def test_coordinates_are_named_fields_not_a_positional_pair() -> None:
    """INV-95: (lat, lon) の順序取り違えの余地がある tuple / `coordinates` を受け取らない。"""
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(coordinates=[136.4667, 35.3667]))
    payload = _place()
    del payload["lat"]
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(payload)


def test_only_epsg4326_is_accepted() -> None:
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(crs="EPSG:3857"))


def test_bbox_is_named_ordered_and_in_range() -> None:
    with pytest.raises(ValidationError):
        GeoBBox(min_lon=138.0, min_lat=34.0, max_lon=134.0, max_lat=36.0)
    with pytest.raises(ValidationError):
        GeoBBox(min_lon=134.0, min_lat=34.0, max_lon=138.0, max_lat=91.0)
    with pytest.raises(ValidationError):
        GeoBBox.model_validate({"bbox": [134.0, 34.0, 138.0, 36.0]})


def test_valid_years_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(valid_start_year=1700, valid_end_year=1500))


def test_a_real_map_place_needs_a_claim_and_sources() -> None:
    """地図上の実在地点は主張（claim）と資料に紐づく。紐づけの無い点を作れない。"""
    for missing in ("claim_id", "source_ids"):
        place = _place(**{missing: None if missing == "claim_id" else []})
        with pytest.raises(ValidationError):
            GeoManifest.model_validate(_manifest(places=[place]))


def test_claim_and_source_ids_use_the_research_id_patterns() -> None:
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(claim_id="claim-1"))
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(source_ids=["src"]))


def test_duplicate_place_ids_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(places=[_place(), _place()]))


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(zoom=3))


def test_map_kind_is_a_closed_set() -> None:
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(map_kind="photo"))


def test_a_schematic_manifest_without_a_disclaimer_is_constructible_for_the_verifier() -> None:
    """INV-96 は検証器の BLOCK で強制する。作れないのではなく、BLOCK として記録に残す設計。"""
    manifest = GeoManifest.model_validate(
        _manifest(map_kind="schematic", places=[], labels=[], as_of="")
    )
    assert manifest.schematic_disclaimer is None


def test_border_basis_year_is_required_iff_historical() -> None:
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(borders="historical"))
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(borders="modern", border_valid_year=1600))
    ok = GeoManifest.model_validate(_manifest(borders="historical", border_valid_year=1600))
    assert ok.border_valid_year == 1600


def test_render_sha256_must_be_a_sha256_when_present() -> None:
    with pytest.raises(ValidationError):
        GeoManifest.model_validate(_manifest(render_sha256="abc"))
    GeoManifest.model_validate(_manifest(render_sha256="a" * 64))


def test_label_is_place_id_and_text() -> None:
    label = GeoLabel(place_id="sekigahara", text="Sekigahara")
    assert label.text == "Sekigahara"
    with pytest.raises(ValidationError):
        GeoLabel(place_id="sekigahara", text="")


def test_the_verdict_order_is_pass_warn_review_block() -> None:
    order = [GeoVerdict.PASS, GeoVerdict.WARN, GeoVerdict.REVIEW, GeoVerdict.BLOCK]
    for i, a in enumerate(order):
        for j, b in enumerate(order):
            assert stricter(a, b) is order[max(i, j)]


def test_a_verification_cannot_be_milder_than_its_worst_check() -> None:
    """INV-94 の契約側: BLOCK の check を含む記録に PASS を付けられない。"""
    checks = (
        GeoCheck(name="a", outcome=GeoVerdict.PASS, detail="ok"),
        GeoCheck(name="b", outcome=GeoVerdict.BLOCK, detail="off by 280 km"),
    )
    with pytest.raises(ValidationError):
        GeoVerification(verdict=GeoVerdict.PASS, checks=checks)
    with pytest.raises(ValidationError):
        GeoVerification(verdict=GeoVerdict.REVIEW, checks=checks)
    assert GeoVerification(verdict=GeoVerdict.BLOCK, checks=checks).verdict is GeoVerdict.BLOCK
    # 厳しすぎる側（check より重い verdict）も作らない: verdict は check から導く値
    with pytest.raises(ValidationError):
        GeoVerification(verdict=GeoVerdict.BLOCK, checks=checks[:1])


def test_a_verification_needs_at_least_one_check() -> None:
    with pytest.raises(ValidationError):
        GeoVerification(verdict=GeoVerdict.PASS, checks=())


def test_disclaimer_vocabulary_includes_not_to_scale() -> None:
    from contracts.geo import SCHEMATIC_DISCLAIMER_PHRASES

    assert "not to scale" in SCHEMATIC_DISCLAIMER_PHRASES


# ---- レビュー指摘: m2 厳格な数値、B1 申告の上限、M3 表示語の定義


@pytest.mark.parametrize("value", [True, False, "35.3667", "35"])
def test_coordinates_reject_bool_and_string_coercion(value: object) -> None:
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(lat=value))
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(lon=value))


def test_the_declared_uncertainty_radius_has_a_hard_upper_bound() -> None:
    """B1: 自己申告の半径は契約でも上限を持つ（20000 km は作れない）。"""
    from contracts.geo import UNCERTAINTY_RADIUS_MAX_KM

    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(uncertainty_radius_km=20000.0))
    with pytest.raises(ValidationError):
        GeoPlace.model_validate(_place(uncertainty_radius_km=UNCERTAINTY_RADIUS_MAX_KM + 0.1))
    GeoPlace.model_validate(_place(uncertainty_radius_km=UNCERTAINTY_RADIUS_MAX_KM))


def test_the_disclaimer_vocabulary_is_defined_once_in_contracts() -> None:
    from contracts.geo import schematic_disclaimer_ok

    assert schematic_disclaimer_ok("Not to scale")
    assert not schematic_disclaimer_ok("to scale")
    assert not schematic_disclaimer_ok(None)
