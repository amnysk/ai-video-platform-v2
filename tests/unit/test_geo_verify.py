"""domain/geo の決定論的検証（ADR-0071、INV-93 / INV-96、設計書 v3 のテスト T07〜T11）。

理由は docs/testing/geo-contracts-rationale.md。fake gazetteer（ネットワークなし）だけを使う。
"""

from __future__ import annotations

import pytest

from contracts.geo import GeoManifest, GeoVerdict, GeoVerification, ReferencePlace
from contracts.research_evidence import ClaimAssessment
from domain.geo import verify_manifest, verify_scene
from tests.support import fake_gazetteer as fg


def _verify(m: GeoManifest) -> GeoVerification:
    return verify_manifest(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS)


def _outcome(result: GeoVerification, name: str) -> GeoVerdict:
    (found,) = [c for c in result.checks if c.name == name]
    return found.outcome


def test_a_correct_map_passes() -> None:
    result = _verify(fg.manifest())
    assert result.verdict is GeoVerdict.PASS
    assert all(c.outcome is GeoVerdict.PASS for c in result.checks)


def test_verification_is_deterministic() -> None:
    m = fg.manifest()
    assert _verify(m) == _verify(m)
    assert _verify(m).model_dump() == _verify(fg.manifest()).model_dump()


# ------------------------------------------------------------------ T07 距離


def test_a_pin_hundreds_of_km_off_is_blocked() -> None:
    """T07: 関ヶ原を東京の位置に描く（約 280 km）。"""
    m = fg.manifest(places=[fg.place(lat=35.6895, lon=139.6917)])
    result = _verify(m)
    assert result.verdict is GeoVerdict.BLOCK
    assert _outcome(result, "gz:sekigahara.distance") is GeoVerdict.BLOCK


def test_the_distance_tolerance_depends_on_the_feature_type() -> None:
    """同じ約 33 km のずれが、城なら BLOCK・地方（重心）なら PASS。一律の km 閾値ではない。"""
    castle = fg.place(
        place_id="gz:osaka-castle",
        display_name="Osaka Castle",
        feature_type="structure",
        lat=34.6873 + 0.3,
        lon=135.5262,
        valid_start_year=1583,
    )
    region = fg.place(
        place_id="gz:honshu",
        display_name="Honshu",
        feature_type="region",
        lat=36.0 + 0.3,
        lon=138.0,
        valid_start_year=None,
    )
    r_castle = _verify(
        fg.manifest(
            places=[castle], labels=[{"place_id": "gz:osaka-castle", "text": "Osaka Castle"}]
        )
    )
    r_region = _verify(
        fg.manifest(places=[region], labels=[{"place_id": "gz:honshu", "text": "Honshu"}])
    )
    assert _outcome(r_castle, "gz:osaka-castle.distance") is GeoVerdict.BLOCK
    assert _outcome(r_region, "gz:honshu.distance") is GeoVerdict.PASS


def test_lower_location_confidence_widens_the_tolerance() -> None:
    """約 25 km のずれ: confidence=high なら REVIEW 以上、low なら許容内に近づく（段階が緩む）。"""
    shifted = {"lat": 35.3667 + 0.225}  # 約 25 km 北
    high = _verify(fg.manifest(places=[fg.place(**shifted)]))
    low = _verify(fg.manifest(places=[fg.place(location_confidence="low", **shifted)]))
    order = [GeoVerdict.PASS, GeoVerdict.WARN, GeoVerdict.REVIEW, GeoVerdict.BLOCK]
    h = order.index(_outcome(high, "gz:sekigahara.distance"))
    lo = order.index(_outcome(low, "gz:sekigahara.distance"))
    assert h > lo


def test_a_source_uncertainty_radius_widens_the_tolerance() -> None:
    shifted = {"lat": 35.3667 + 0.225}
    tight = _outcome(_verify(fg.manifest(places=[fg.place(**shifted)])), "gz:sekigahara.distance")
    wide = _outcome(
        _verify(fg.manifest(places=[fg.place(uncertainty_radius_km=30.0, **shifted)])),
        "gz:sekigahara.distance",
    )
    assert wide is GeoVerdict.PASS
    assert tight is not GeoVerdict.PASS


def test_a_slightly_large_offset_is_a_warning_not_a_block() -> None:
    """許容の 1 倍超 1.5 倍以内は WARN（報告のみ。ゲートは止めない）。"""
    # 戦場の許容は (15 + 3) * 1.0 = 18 km。約 22 km 北
    result = _verify(fg.manifest(places=[fg.place(lat=35.3667 + 0.2)]))
    assert _outcome(result, "gz:sekigahara.distance") is GeoVerdict.WARN
    assert result.verdict is GeoVerdict.WARN


# ------------------------------------------------------------------ T09 軸順


def test_lat_lon_swap_is_blocked_by_bbox_and_reference() -> None:
    """T09: コンスタンティノープル（41.0N, 29.0E）を (29.0, 41.0) として描く。範囲内の入れ替え。"""
    swapped = fg.place(
        place_id="gz:constantinople",
        display_name="Constantinople",
        feature_type="settlement",
        lat=28.9784,
        lon=41.0082,
        valid_start_year=330,
    )
    m = fg.manifest(
        bbox=fg.BYZANTINE_BBOX,
        as_of="1204",
        places=[swapped],
        labels=[{"place_id": "gz:constantinople", "text": "Constantinople"}],
    )
    result = _verify(m)
    assert result.verdict is GeoVerdict.BLOCK
    assert _outcome(result, "gz:constantinople.swap") is GeoVerdict.BLOCK


def test_swap_is_detected_from_the_reference_even_when_the_bbox_is_wrong_too() -> None:
    """想定 bbox が広すぎて入れ替えた点も含んでしまっても、参照位置との照合で拾う。"""
    swapped = fg.place(
        place_id="gz:constantinople",
        display_name="Constantinople",
        feature_type="settlement",
        lat=28.9784,
        lon=41.0082,
        valid_start_year=330,
    )
    wide = {"min_lon": 0.0, "min_lat": 0.0, "max_lon": 90.0, "max_lat": 90.0}
    m = fg.manifest(
        bbox=wide,
        as_of="1204",
        places=[swapped],
        labels=[{"place_id": "gz:constantinople", "text": "Constantinople"}],
    )
    assert _outcome(_verify(m), "gz:constantinople.swap") is GeoVerdict.BLOCK


def test_a_swap_that_leaves_the_valid_range_cannot_even_be_built() -> None:
    """日本の座標の入れ替えは緯度 136 になり、契約（INV-95）で作れない。"""
    with pytest.raises(ValueError, match="lat"):
        fg.manifest(places=[fg.place(lat=136.4667, lon=35.3667)])


# ------------------------------------------------------------------ T08 時代・同名異所・国境


def test_old_place_name_with_modern_coordinates_is_valid() -> None:
    """T08: 1600 年の「江戸」。参照は現在の東京と同じ座標で、旧称の期間内なので PASS。"""
    edo = fg.place(
        place_id="gz:edo-tokyo",
        display_name="Edo",
        feature_type="settlement",
        lat=35.6895,
        lon=139.6917,
        valid_start_year=1457,
    )
    m = fg.manifest(places=[edo], labels=[{"place_id": "gz:edo-tokyo", "text": "Edo"}])
    assert _verify(m).verdict is GeoVerdict.PASS


def test_a_modern_name_on_a_historical_map_needs_review() -> None:
    """1600 年の地図に「Tokyo」（1868 年からの名）。位置は正しくても名前が時代に合わない。"""
    tokyo = fg.place(
        place_id="gz:edo-tokyo",
        display_name="Tokyo",
        feature_type="settlement",
        lat=35.6895,
        lon=139.6917,
        valid_start_year=1457,
    )
    m = fg.manifest(places=[tokyo], labels=[{"place_id": "gz:edo-tokyo", "text": "Tokyo"}])
    result = _verify(m)
    assert _outcome(result, "gz:edo-tokyo.name_era") is GeoVerdict.REVIEW
    assert result.verdict is GeoVerdict.REVIEW


def test_the_same_modern_name_is_fine_on_a_modern_context_map() -> None:
    tokyo = fg.place(
        place_id="gz:edo-tokyo",
        display_name="Tokyo",
        feature_type="settlement",
        lat=35.6895,
        lon=139.6917,
        valid_start_year=1457,
    )
    m = fg.manifest(
        map_kind="modern_context",
        as_of="2026",
        places=[tokyo],
        labels=[{"place_id": "gz:edo-tokyo", "text": "Tokyo"}],
    )
    assert _verify(m).verdict is GeoVerdict.PASS


def test_a_homonym_is_never_auto_picked() -> None:
    """T08: 「Fuchu」の参照が 2 件。片方に近くても自動で選ばず REVIEW。"""
    fuchu = fg.place(
        place_id="fuchu",
        display_name="Fuchu",
        feature_type="settlement",
        lat=35.6689,
        lon=139.4778,
        valid_start_year=None,
    )
    m = fg.manifest(places=[fuchu], labels=[{"place_id": "fuchu", "text": "Fuchu"}])
    result = _verify(m)
    assert _outcome(result, "fuchu.reference") is GeoVerdict.REVIEW
    assert result.verdict is GeoVerdict.REVIEW


def test_an_explicit_catalog_id_resolves_a_homonym_and_the_distance_still_applies() -> None:
    right = fg.place(
        place_id="gz:fuchu-tokyo",
        display_name="Fuchu",
        feature_type="settlement",
        lat=35.6689,
        lon=139.4778,
        valid_start_year=None,
    )
    m = fg.manifest(places=[right], labels=[{"place_id": "gz:fuchu-tokyo", "text": "Fuchu"}])
    assert _verify(m).verdict is GeoVerdict.PASS
    wrong = {**right, "lat": 34.5686, "lon": 133.2367}  # 広島の府中の位置に東京の id
    m2 = fg.manifest(places=[wrong], labels=[{"place_id": "gz:fuchu-tokyo", "text": "Fuchu"}])
    assert _verify(m2).verdict is GeoVerdict.BLOCK


def test_a_place_without_any_reference_cannot_pass() -> None:
    nowhere = fg.place(place_id="x1", display_name="Atlantis", valid_start_year=None)
    m = fg.manifest(places=[nowhere], labels=[{"place_id": "x1", "text": "Atlantis"}])
    result = _verify(m)
    assert _outcome(result, "x1.reference") is GeoVerdict.REVIEW


def test_modern_borders_on_a_historical_map_need_review() -> None:
    """T08: 当時の資料の無い現代の国境を歴史地図に重ねる。"""
    result = _verify(fg.manifest(borders="modern"))
    assert _outcome(result, "borders") is GeoVerdict.REVIEW
    assert result.verdict is GeoVerdict.REVIEW


def test_a_border_assertion_without_border_data_needs_review() -> None:
    m = fg.manifest(spatial_assertions=["The border between A and B runs along the river"])
    assert _outcome(_verify(m), "borders") is GeoVerdict.REVIEW


def test_historical_borders_must_match_the_map_time_basis() -> None:
    ok = fg.manifest(borders="historical", border_valid_year=1600)
    far = fg.manifest(borders="historical", border_valid_year=1900)
    assert _outcome(_verify(ok), "borders") is GeoVerdict.PASS
    assert _outcome(_verify(far), "borders") is GeoVerdict.REVIEW


def test_modern_borders_are_fine_on_a_modern_context_map() -> None:
    m = fg.manifest(
        map_kind="modern_context",
        as_of="2026",
        borders="modern",
        places=[fg.place(valid_end_year=None)],
    )
    assert _outcome(_verify(m), "borders") is GeoVerdict.PASS


# ------------------------------------------------------------------ 時間基準


def test_an_unparseable_time_basis_needs_review() -> None:
    for as_of in ("", "around 1600", "1600-13", "16000"):
        result = _verify(fg.manifest(as_of=as_of))
        assert _outcome(result, "as_of") is GeoVerdict.REVIEW, as_of


@pytest.mark.parametrize("as_of", ["1600", "1600-10", "1600-10-21", "-0200"])
def test_time_basis_formats_are_accepted(as_of: str) -> None:
    place = fg.place(valid_start_year=-300)
    assert _outcome(_verify(fg.manifest(as_of=as_of, places=[place])), "as_of") is GeoVerdict.PASS


def test_a_place_outside_its_valid_period_is_blocked_on_a_historical_map() -> None:
    """存在しなかった時代の地物を「実際の位置」として描く（設計書 §3.3 BLOCK）。"""
    m = fg.manifest(as_of="1100", places=[fg.place(valid_start_year=1580, valid_end_year=1650)])
    result = _verify(m)
    assert _outcome(result, "gz:sekigahara.valid_time") is GeoVerdict.BLOCK


def test_the_references_own_period_also_applies() -> None:
    """大坂城は 1583 年から。place 側が期間を省いても参照側の期間で止まる。"""
    castle = fg.place(
        place_id="gz:osaka-castle",
        display_name="Osaka Castle",
        feature_type="structure",
        lat=34.6873,
        lon=135.5262,
        valid_start_year=None,
    )
    m = fg.manifest(
        as_of="1500",
        places=[castle],
        labels=[{"place_id": "gz:osaka-castle", "text": "Osaka Castle"}],
    )
    assert _outcome(_verify(m), "gz:osaka-castle.valid_time") is GeoVerdict.REVIEW


# ------------------------------------------------------------------ T10 ラベル


def test_a_label_for_an_unknown_place_is_blocked() -> None:
    m = fg.manifest(labels=[{"place_id": "gz:nowhere", "text": "Nowhere"}])
    result = _verify(m)
    assert _outcome(result, "label[gz:nowhere]") is GeoVerdict.BLOCK


def test_a_misspelled_label_is_blocked() -> None:
    """T10: 地図は正しいが文字が誤字（Sekigahra）。"""
    m = fg.manifest(labels=[{"place_id": "gz:sekigahara", "text": "Sekigahra"}])
    result = _verify(m)
    assert _outcome(result, "label[gz:sekigahara]") is GeoVerdict.BLOCK
    assert result.verdict is GeoVerdict.BLOCK


def test_labels_match_by_normalized_display_name_or_a_known_alias() -> None:
    for text in ("SEKIGAHARA", " Sekigahara ", "関ヶ原"):
        m = fg.manifest(labels=[{"place_id": "gz:sekigahara", "text": text}])
        assert _outcome(_verify(m), "label[gz:sekigahara]") is GeoVerdict.PASS, text


# ------------------------------------------------------------------ claim の解決


@pytest.mark.parametrize("assessment", [ClaimAssessment.DISPUTED, ClaimAssessment.INSUFFICIENT])
def test_a_claim_that_is_not_supported_needs_review(assessment: ClaimAssessment) -> None:
    claims = (fg.claim("C-001", assessment),)
    result = verify_manifest(fg.manifest(), fg.ALL_REFERENCES, claims)
    assert _outcome(result, "gz:sekigahara.claim") is GeoVerdict.REVIEW


def test_a_qualified_claim_resolves() -> None:
    claims = (fg.claim("C-001", ClaimAssessment.QUALIFIED),)
    result = verify_manifest(fg.manifest(), fg.ALL_REFERENCES, claims)
    assert result.verdict is GeoVerdict.PASS


def test_a_claim_id_that_does_not_exist_needs_review() -> None:
    result = verify_manifest(fg.manifest(), fg.ALL_REFERENCES, ())
    assert _outcome(result, "gz:sekigahara.claim") is GeoVerdict.REVIEW


# ------------------------------------------------------------------ T11 / INV-96 模式図


def _schematic(**over: object) -> GeoManifest:
    base: dict[str, object] = {
        "map_kind": "schematic",
        "as_of": "",
        "places": [],
        "labels": [],
    }
    base.update(over)
    return fg.manifest(**base)


@pytest.mark.parametrize("disclaimer", [None, "", "   ", "A schematic map", "Not drawn"])
def test_a_schematic_without_the_disclaimer_is_blocked(disclaimer: str | None) -> None:
    """T11 / INV-96。"""
    result = _verify(_schematic(schematic_disclaimer=disclaimer))
    assert _outcome(result, "schematic_disclaimer") is GeoVerdict.BLOCK
    assert result.verdict is GeoVerdict.BLOCK


@pytest.mark.parametrize(
    "disclaimer", ["Schematic / Not to scale", "模式図（縮尺不同）", "NOT TO SCALE"]
)
def test_a_schematic_with_the_disclaimer_passes(disclaimer: str) -> None:
    assert _verify(_schematic(schematic_disclaimer=disclaimer)).verdict is GeoVerdict.PASS


def test_a_schematic_is_not_checked_against_real_locations() -> None:
    """模式図は実在の位置を主張しないので距離・時代は見ない（誤った位置でも BLOCK にしない）。"""
    off = fg.place(lat=35.6895, lon=139.6917, claim_id=None, source_ids=[])
    m = _schematic(schematic_disclaimer="Not to scale", places=[off])
    assert _verify(m).verdict is GeoVerdict.PASS


def test_a_schematic_still_blocks_a_label_for_an_unknown_place() -> None:
    m = _schematic(
        schematic_disclaimer="Not to scale", labels=[{"place_id": "zz", "text": "Anywhere"}]
    )
    assert _verify(m).verdict is GeoVerdict.BLOCK


# ------------------------------------------------------------------ INV-93 manifest の要否


def test_a_scene_claiming_a_real_location_without_a_manifest_is_blocked() -> None:
    result = verify_scene(True, None, fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    assert result.verdict is GeoVerdict.BLOCK
    assert _outcome(result, "manifest_required") is GeoVerdict.BLOCK


def test_a_scene_without_a_real_location_claim_needs_no_manifest() -> None:
    assert verify_scene(False, None, fg.ALL_REFERENCES, fg.GOOD_CLAIMS).verdict is GeoVerdict.PASS


def test_a_schematic_cannot_back_a_real_location_claim() -> None:
    m = _schematic(schematic_disclaimer="Not to scale")
    result = verify_scene(True, m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    assert result.verdict is GeoVerdict.BLOCK


def test_verify_scene_delegates_to_the_manifest_checks() -> None:
    m = fg.manifest(places=[fg.place(lat=35.6895, lon=139.6917)])
    assert verify_scene(True, m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS).verdict is GeoVerdict.BLOCK
    assert verify_scene(True, fg.manifest(), fg.ALL_REFERENCES, fg.GOOD_CLAIMS).verdict is (
        GeoVerdict.PASS
    )


def test_a_non_schematic_map_with_no_places_cannot_pass() -> None:
    result = _verify(fg.manifest(places=[], labels=[]))
    assert result.verdict is GeoVerdict.REVIEW


def test_the_verifier_does_not_mutate_its_inputs() -> None:
    refs: tuple[ReferencePlace, ...] = fg.ALL_REFERENCES
    before = [r.model_dump() for r in refs]
    _verify(fg.manifest())
    assert [r.model_dump() for r in refs] == before
