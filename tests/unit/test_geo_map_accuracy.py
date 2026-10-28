"""地図の歴史的正確さの判定（ADR-0078、INV-117）。fake gazetteer だけを使う。

理由は docs/testing/geo-maps-rationale.md。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contracts.geo import GeoManifest, GeoVerdict, GeoVerification, MapKind, stricter
from contracts.geo_map import (
    MapAccuracyResult,
    MapFinding,
    MapReasonCode,
    MapTargetKind,
)
from domain.geo.map_accuracy import check_map_accuracy, check_scene_map_accuracy, escalate
from tests.support import fake_gazetteer as fg


def _check(m: GeoManifest, **kw: object) -> MapAccuracyResult:
    return check_map_accuracy(m, fg.ALL_REFERENCES, fg.GOOD_CLAIMS, fg.GOOD_SOURCES, **kw)  # type: ignore[arg-type]


def _codes(result: MapAccuracyResult) -> set[tuple[MapReasonCode, GeoVerdict]]:
    return {(f.reason_code, f.verdict) for f in result.findings}


def test_a_correct_map_passes_with_a_single_ok_finding() -> None:
    result = _check(fg.manifest())
    assert result.verdict is GeoVerdict.PASS
    assert [f.reason_code for f in result.findings] == [MapReasonCode.OK]


def test_misplaced_coordinates_are_blocked_with_the_place_as_target() -> None:
    """関ヶ原を東京の位置に描いた（約 280 km）。"""
    result = _check(fg.manifest(places=[fg.place(lat=35.6895, lon=139.6917)]))
    assert result.verdict is GeoVerdict.BLOCK
    (finding,) = [f for f in result.findings if f.reason_code is MapReasonCode.POSITION_MISMATCH]
    assert finding.verdict is GeoVerdict.BLOCK
    assert (finding.target_kind, finding.target_id) == (MapTargetKind.PLACE, "gz:sekigahara")
    assert finding.evidence_claim_id == "C-001"
    assert finding.evidence_source_ids == ("S-001",)


def test_swapped_axes_are_blocked_with_their_own_reason() -> None:
    result = _check(
        fg.manifest(
            places=[
                fg.place(
                    place_id="gz:constantinople",
                    display_name="Constantinople",
                    lat=28.9784,
                    lon=41.0082,
                    feature_type="settlement",
                    valid_start_year=330,
                )
            ],
            labels=[],
            as_of="1200",
            bbox=fg.BYZANTINE_BBOX,
        )
    )
    assert result.verdict is GeoVerdict.BLOCK
    assert MapReasonCode.AXES_SWAPPED in {f.reason_code for f in result.findings}


def test_an_anachronistic_place_name_on_a_historical_map_is_blocked() -> None:
    """1600 年の地図に Tokyo（1868 年以降の名）。検証器は REVIEW、地図の関門は BLOCK。"""
    m = fg.manifest(
        places=[
            fg.place(
                place_id="gz:edo-tokyo",
                display_name="Tokyo",
                lat=35.6895,
                lon=139.6917,
                feature_type="settlement",
                valid_start_year=None,
            )
        ],
        labels=[],
    )
    result = _check(m)
    assert result.verdict is GeoVerdict.BLOCK
    assert (MapReasonCode.ANACHRONISTIC_NAME, GeoVerdict.BLOCK) in _codes(result)


def test_an_anachronistic_label_is_blocked() -> None:
    m = fg.manifest(
        places=[
            fg.place(
                place_id="gz:edo-tokyo",
                display_name="Edo",
                lat=35.6895,
                lon=139.6917,
                feature_type="settlement",
                valid_start_year=None,
            )
        ],
        labels=[{"place_id": "gz:edo-tokyo", "text": "Tokyo"}],
    )
    assert (MapReasonCode.ANACHRONISTIC_LABEL, GeoVerdict.BLOCK) in _codes(_check(m))


def test_a_place_that_did_not_exist_yet_is_blocked() -> None:
    """大坂城（1583 年〜）を 1500 年の地図に置く。"""
    m = fg.manifest(
        as_of="1500",
        places=[
            fg.place(
                place_id="gz:osaka-castle",
                display_name="Osaka Castle",
                lat=34.6873,
                lon=135.5262,
                feature_type="structure",
                valid_start_year=1583,
            )
        ],
        labels=[],
    )
    assert (MapReasonCode.ANACHRONISTIC_PLACE, GeoVerdict.BLOCK) in _codes(_check(m))


def test_modern_borders_on_a_historical_map_are_blocked() -> None:
    result = _check(fg.manifest(borders="modern"))
    assert result.verdict is GeoVerdict.BLOCK
    (finding,) = [f for f in result.findings if f.reason_code is MapReasonCode.MODERN_BORDER]
    assert (finding.target_kind, finding.verdict) == (MapTargetKind.MANIFEST, GeoVerdict.BLOCK)
    assert finding.target_id == "s1"


def test_modern_borders_on_a_modern_context_map_are_not_an_accuracy_error() -> None:
    result = _check(fg.manifest(map_kind="modern_context", as_of="2024", borders="modern"))
    assert MapReasonCode.MODERN_BORDER not in {f.reason_code for f in result.findings}


def test_historical_borders_of_another_era_are_blocked() -> None:
    result = _check(fg.manifest(borders="historical", border_valid_year=1900))
    assert (MapReasonCode.BORDER_ERA_MISMATCH, GeoVerdict.BLOCK) in _codes(result)


def test_a_border_claim_without_a_border_basis_is_blocked() -> None:
    result = _check(fg.manifest(spatial_assertions=["The border of the realm ran along the river"]))
    assert (MapReasonCode.BORDER_UNSUPPORTED, GeoVerdict.BLOCK) in _codes(result)


def test_a_place_whose_claim_does_not_resolve_is_blocked_as_missing_evidence() -> None:
    result = check_map_accuracy(fg.manifest(), fg.ALL_REFERENCES, (), fg.GOOD_SOURCES)
    assert result.verdict is GeoVerdict.BLOCK
    (finding,) = [f for f in result.findings if f.reason_code is MapReasonCode.EVIDENCE_MISSING]
    assert finding.target_id == "gz:sekigahara"
    assert finding.evidence_claim_id == "C-001"


def test_a_place_whose_source_was_not_fetched_is_blocked_as_missing_evidence() -> None:
    unfetched = (fg.source("S-001", fg.SourceFetchStatus.FAILED),)
    result = check_map_accuracy(fg.manifest(), fg.ALL_REFERENCES, fg.GOOD_CLAIMS, unfetched)
    assert (MapReasonCode.SOURCE_MISSING, GeoVerdict.BLOCK) in _codes(result)


def test_an_unsupported_claim_is_not_evidence() -> None:
    weak = (fg.claim("C-001", fg.ClaimAssessment.INSUFFICIENT),)
    result = check_map_accuracy(fg.manifest(), fg.ALL_REFERENCES, weak, fg.GOOD_SOURCES)
    assert result.verdict is GeoVerdict.BLOCK


def test_a_place_without_a_claim_cannot_even_be_built() -> None:
    """契約が先に止める（fail-closed）。検証器へ届く前の層。"""
    with pytest.raises(ValidationError):
        fg.manifest(places=[fg.place(claim_id=None)])


def test_a_homonym_stays_a_review_and_is_never_auto_picked() -> None:
    m = fg.manifest(
        places=[
            fg.place(
                place_id="fuchu-x",
                display_name="Fuchu",
                lat=35.6689,
                lon=139.4778,
                feature_type="settlement",
                valid_start_year=None,
            )
        ],
        labels=[],
    )
    result = _check(m)
    assert result.verdict is GeoVerdict.REVIEW
    assert (MapReasonCode.AMBIGUOUS_PLACE, GeoVerdict.REVIEW) in _codes(result)


def test_an_id_that_names_the_reference_disambiguates_a_homonym() -> None:
    m = fg.manifest(
        places=[
            fg.place(
                place_id="gz:fuchu-tokyo",
                display_name="Fuchu",
                lat=35.6689,
                lon=139.4778,
                feature_type="settlement",
                valid_start_year=None,
            )
        ],
        labels=[],
    )
    assert _check(m).verdict is GeoVerdict.PASS


def test_an_unknown_place_is_a_review_not_a_pass() -> None:
    m = fg.manifest(
        places=[fg.place(place_id="nowhere", display_name="Atlantis", valid_start_year=None)],
        labels=[],
    )
    result = _check(m)
    assert result.verdict is GeoVerdict.REVIEW
    assert (MapReasonCode.UNKNOWN_PLACE, GeoVerdict.REVIEW) in _codes(result)


def test_an_unreadable_time_basis_is_a_review() -> None:
    result = _check(fg.manifest(as_of="sometime"))
    assert (MapReasonCode.AS_OF_UNKNOWN, GeoVerdict.REVIEW) in _codes(result)


def test_a_schematic_without_the_disclaimer_is_blocked_with_its_own_reason() -> None:
    m = fg.manifest(map_kind="schematic", schematic_disclaimer=None, places=[], labels=[])
    result = _check(m)
    assert (MapReasonCode.SCHEMATIC_DISCLAIMER_MISSING, GeoVerdict.BLOCK) in _codes(result)


def test_a_labelled_schematic_passes() -> None:
    m = fg.manifest(
        map_kind="schematic", schematic_disclaimer="模式図・縮尺不同", places=[], labels=[]
    )
    assert _check(m).verdict is GeoVerdict.PASS


def test_a_scene_claiming_a_real_location_without_a_manifest_is_blocked() -> None:
    result = check_scene_map_accuracy(True, None, "s1", fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    assert result.verdict is GeoVerdict.BLOCK
    assert [f.reason_code for f in result.findings] == [MapReasonCode.MANIFEST_REQUIRED]
    assert result.findings[0].target_id == "s1"


def test_a_schematic_presented_as_a_real_location_is_blocked() -> None:
    m = fg.manifest(map_kind="schematic", schematic_disclaimer="Not to scale", places=[], labels=[])
    result = check_scene_map_accuracy(True, m, "s1", fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    assert (MapReasonCode.SCHEMATIC_AS_REAL, GeoVerdict.BLOCK) in _codes(result)


def test_a_scene_without_a_map_claim_passes() -> None:
    result = check_scene_map_accuracy(False, None, "s1", fg.ALL_REFERENCES, fg.GOOD_CLAIMS)
    assert result.verdict is GeoVerdict.PASS


def test_the_result_is_deterministic_and_carries_no_free_text() -> None:
    m = fg.manifest(places=[fg.place(lat=35.6895, lon=139.6917)])
    a, b = _check(m), _check(m)
    assert a == b
    assert a.model_dump_json() == b.model_dump_json()
    assert set(MapFinding.model_fields) == {
        "verdict",
        "reason_code",
        "target_kind",
        "target_id",
        "evidence_claim_id",
        "evidence_source_ids",
    }


def test_the_result_verdict_cannot_be_milder_than_its_worst_finding() -> None:
    blocked = MapFinding(
        verdict=GeoVerdict.BLOCK,
        reason_code=MapReasonCode.POSITION_MISMATCH,
        target_kind=MapTargetKind.PLACE,
        target_id="p1",
    )
    with pytest.raises(ValidationError):
        MapAccuracyResult(scene_id="s1", verdict=GeoVerdict.PASS, findings=(blocked,))


def test_a_non_pass_finding_needs_a_real_reason_not_ok() -> None:
    with pytest.raises(ValidationError):
        MapFinding(
            verdict=GeoVerdict.BLOCK,
            reason_code=MapReasonCode.OK,
            target_kind=MapTargetKind.MANIFEST,
            target_id="s1",
        )


def test_every_non_pass_verifier_check_is_classified() -> None:
    """検証器の check 名の語彙が増えたのに分類表が追随しないと UNCLASSIFIED になる（通報）。"""
    scenarios = [
        fg.manifest(places=[fg.place(lat=35.6895, lon=139.6917)]),
        fg.manifest(borders="modern"),
        fg.manifest(as_of="sometime"),
        fg.manifest(places=[fg.place(display_name="Kyoto")]),
    ]
    for m in scenarios:
        result = _check(m)
        assert MapReasonCode.UNCLASSIFIED not in {f.reason_code for f in result.findings}


@pytest.mark.parametrize("code", list(MapReasonCode))
@pytest.mark.parametrize("kind", [*MapKind, None])
@pytest.mark.parametrize("outcome", list(GeoVerdict))
def test_escalation_never_relaxes_any_verdict(
    code: MapReasonCode, kind: MapKind | None, outcome: GeoVerdict
) -> None:
    assert stricter(escalate(code, outcome, kind), outcome) is escalate(code, outcome, kind)
    # 引き上げは REVIEW からだけ。WARN / PASS / BLOCK は動かない。
    if outcome is not GeoVerdict.REVIEW:
        assert escalate(code, outcome, kind) is outcome


def _all_failing_manifest() -> GeoManifest:
    places = [
        fg.place(
            place_id=f"p{i}",
            display_name="Atlantis",
            lat=35.0,
            lon=136.0,
            valid_start_year=2000,
            location_confidence="low",
        )
        for i in range(200)
    ]
    labels = [{"place_id": f"p{i}", "text": "Nope"} for i in range(200)]
    return fg.manifest(places=places, labels=labels, borders="modern")


def test_a_huge_failing_manifest_still_yields_a_result_not_an_exception() -> None:
    result = check_map_accuracy(_all_failing_manifest(), fg.ALL_REFERENCES, (), fg.GOOD_SOURCES)
    assert result.verdict is GeoVerdict.BLOCK


def test_too_many_findings_are_capped_with_a_block_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    import domain.geo.map_accuracy as ma

    monkeypatch.setattr(ma, "MAX_FINDINGS", 10)
    result = check_map_accuracy(_all_failing_manifest(), fg.ALL_REFERENCES, (), fg.GOOD_SOURCES)
    assert result.verdict is GeoVerdict.BLOCK
    assert len(result.findings) == 11
    assert result.findings[-1].reason_code is MapReasonCode.TOO_MANY_FINDINGS
    assert result.findings[-1].verdict is GeoVerdict.BLOCK
    # 残すのは厳しい順（BLOCK を落として REVIEW を残さない）
    assert all(f.verdict is GeoVerdict.BLOCK for f in result.findings)


def test_a_verifier_result_too_large_to_build_is_a_block_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import domain.geo.map_accuracy as ma

    def too_big(*args: object, **kwargs: object) -> None:
        GeoVerification(verdict=GeoVerdict.PASS, checks=())  # 契約違反で ValidationError

    monkeypatch.setattr(ma, "verify_manifest", too_big)
    result = check_map_accuracy(fg.manifest(), fg.ALL_REFERENCES, fg.GOOD_CLAIMS, fg.GOOD_SOURCES)
    assert result.verdict is GeoVerdict.BLOCK
    assert [f.reason_code for f in result.findings] == [MapReasonCode.TOO_MANY_FINDINGS]
