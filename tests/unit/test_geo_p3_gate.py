"""Phase 3 の地図の関門（組み立てと正確さの判定）。理由は docs/testing/v3-phase3-geo.md。"""

from __future__ import annotations

import pytest

from contracts.geo import GeoVerdict, MapKind
from contracts.geo_map import MapReasonCode
from contracts.geo_scene import GeoMapSpec
from contracts.research_evidence import ClaimAssessment, SourceFetchStatus
from domain.geo.manifest_builder import build_manifest
from domain.geo.map_gate import evidence_digest, gate_map, gazetteer_digest
from tests.support import geo_scene as gs


def codes(outcome) -> set[MapReasonCode]:  # type: ignore[no-untyped-def]
    return {f.reason_code for f in outcome.accuracy.findings}


def run(spec: GeoMapSpec, claims=gs.CLAIMS, sources=gs.SOURCES):  # type: ignore[no-untyped-def]
    return gate_map(spec, gs.GAZETTEER, claims, sources)


def test_a_well_formed_request_passes_and_takes_coordinates_from_the_gazetteer() -> None:
    outcome = run(gs.spec())
    assert outcome.passed, outcome.accuracy
    assert outcome.manifest is not None
    by_id = {p.place_id: p for p in outcome.manifest.places}
    assert (by_id["gz:yashima"].lat, by_id["gz:yashima"].lon) == (gs.YASHIMA.lat, gs.YASHIMA.lon)
    assert by_id["gz:kyoto"].valid_start_year is None


def test_the_same_request_always_builds_the_same_manifest() -> None:
    a, _ = build_manifest(gs.request(), gs.GAZETTEER)
    b, _ = build_manifest(gs.request(places=tuple(reversed(gs.request().places))), gs.GAZETTEER)
    assert a is not None and b is not None and a == b


def test_a_request_cannot_carry_coordinates() -> None:
    with pytest.raises(ValueError):
        gs.place_req(lat=10.0, lon=10.0)


def test_an_unknown_gazetteer_reference_is_blocked_not_guessed() -> None:
    outcome = run(gs.spec(places=(gs.place_req("gz:atlantis", "Atlantis"),)))
    assert outcome.manifest is None
    assert outcome.accuracy.verdict is GeoVerdict.BLOCK
    assert codes(outcome) == {MapReasonCode.UNKNOWN_PLACE}


def test_a_duplicated_reference_in_a_request_is_ambiguous() -> None:
    outcome = run(gs.spec(places=(gs.place_req(), gs.place_req())))
    assert MapReasonCode.AMBIGUOUS_PLACE in codes(outcome)
    assert not outcome.passed


def test_a_duplicated_gazetteer_entry_is_ambiguous() -> None:
    outcome = gate_map(gs.spec(), (*gs.GAZETTEER, gs.YASHIMA), gs.CLAIMS, gs.SOURCES)
    assert MapReasonCode.AMBIGUOUS_PLACE in codes(outcome)
    assert not outcome.passed


def test_a_place_without_a_claim_or_without_a_source_is_blocked() -> None:
    no_claim = run(gs.spec(places=(gs.place_req(claim_id=None),)))
    assert codes(no_claim) == {MapReasonCode.EVIDENCE_MISSING}
    no_source = run(gs.spec(places=(gs.place_req(source_ids=()),)))
    assert codes(no_source) == {MapReasonCode.SOURCE_MISSING}
    assert no_claim.accuracy.verdict is no_source.accuracy.verdict is GeoVerdict.BLOCK


def test_a_source_that_is_not_in_the_research_evidence_is_refused() -> None:
    outcome = run(gs.spec(places=(gs.place_req(source_ids=("S-404",)),)))
    assert not outcome.passed
    assert MapReasonCode.SOURCE_MISSING in codes(outcome)


def test_a_source_that_was_not_fetched_is_not_a_citation() -> None:
    failed = (gs.source("S-001", SourceFetchStatus.FAILED),)
    outcome = run(gs.spec(), sources=failed)
    assert not outcome.passed
    assert MapReasonCode.SOURCE_MISSING in codes(outcome)


def test_an_insufficient_claim_is_not_evidence() -> None:
    weak = (gs.claim("C-001", ClaimAssessment.INSUFFICIENT),)
    outcome = run(gs.spec(), claims=weak)
    assert not outcome.passed
    assert codes(outcome) & {MapReasonCode.EVIDENCE_MISSING, MapReasonCode.EVIDENCE_NOT_RELEVANT}


def test_an_unresolved_claim_id_is_refused() -> None:
    outcome = run(gs.spec(places=(gs.place_req(claim_id="C-999"),)))
    assert not outcome.passed


def test_a_modern_name_on_a_twelfth_century_map_is_an_anachronism() -> None:
    outcome = run(
        gs.spec(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),)),
        claims=(gs.claim(),),
    )
    assert outcome.accuracy.verdict is GeoVerdict.BLOCK
    assert codes(outcome) & {
        MapReasonCode.ANACHRONISTIC_NAME,
        MapReasonCode.ANACHRONISTIC_PLACE,
        MapReasonCode.ANACHRONISTIC_LABEL,
    }


def test_a_name_that_belongs_to_another_place_is_blocked() -> None:
    outcome = run(gs.spec(places=(gs.place_req("gz:yashima", "Kyoto"),)))
    assert not outcome.passed


def test_an_unknown_time_basis_on_a_historical_map_is_refused() -> None:
    outcome = run(gs.spec(as_of="twelfth century"))
    assert outcome.accuracy.verdict is not GeoVerdict.PASS  # REVIEW でも地図は使われない
    assert not outcome.passed
    assert MapReasonCode.AS_OF_UNKNOWN in codes(outcome)


def test_a_modern_border_on_a_historical_map_is_refused() -> None:
    outcome = run(gs.spec(borders="modern"))
    assert not outcome.passed
    assert MapReasonCode.MODERN_BORDER in codes(outcome)


def test_a_manifest_with_wrong_coordinates_is_blocked_by_the_verifier() -> None:
    """依頼を経ない manifest（LLM が書いた等）の位置違いを止める。"""
    good = gs.manifest_from(gs.spec())
    moved = good.model_copy(
        update={
            "places": tuple(
                p.model_copy(update={"lat": 40.0, "lon": 140.0})
                if p.place_id == "gz:yashima"
                else p
                for p in good.places
            )
        }
    )
    outcome = run(GeoMapSpec(storyboard_scene_id="sb3", manifest=moved))
    assert outcome.accuracy.verdict is GeoVerdict.BLOCK
    assert codes(outcome) & {MapReasonCode.POSITION_MISMATCH, MapReasonCode.OUT_OF_BOUNDS}


def test_swapped_axes_are_blocked() -> None:
    good = gs.manifest_from(gs.spec())
    swapped = good.model_copy(
        update={
            "places": tuple(
                p.model_copy(update={"lat": p.lon - 100.0, "lon": p.lat + 100.0})
                if p.place_id == "gz:kyoto"
                else p
                for p in good.places
            )
        }
    )
    assert not run(GeoMapSpec(storyboard_scene_id="sb3", manifest=swapped)).passed


def test_a_schematic_map_without_the_disclaimer_is_blocked() -> None:
    outcome = run(gs.spec(map_kind=MapKind.SCHEMATIC, places=()))
    assert codes(outcome) == {MapReasonCode.SCHEMATIC_DISCLAIMER_MISSING}
    assert outcome.accuracy.verdict is GeoVerdict.BLOCK


def test_input_fingerprints_ignore_order_and_change_with_content() -> None:
    assert gazetteer_digest(gs.GAZETTEER) == gazetteer_digest(tuple(reversed(gs.GAZETTEER)))
    assert gazetteer_digest(gs.GAZETTEER) != gazetteer_digest(gs.GAZETTEER[:-1])
    assert evidence_digest(gs.CLAIMS, gs.SOURCES) != evidence_digest(gs.CLAIMS, ())
