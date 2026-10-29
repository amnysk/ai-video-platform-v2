"""地図の証跡の hash 結びつきと、品質検査への変換。理由は docs/testing/v3-phase3-geo.md。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.geo import GeoVerdict
from contracts.geo_scene import GeoMapRecord, GeoMapSpecs
from contracts.quality import QualityGate, QualityReason, QualityVerdict
from domain.geo.digest import sha256_of
from domain.geo.map_binding import BindingProblem, binding_problems, record_sha256
from domain.geo.quality_checks import map_quality_checks, not_used_evidence_ref
from infrastructure.geo.prepare import prepare_map
from infrastructure.geo.source import GeoSourceData
from infrastructure.media.map_render import MapRenderSpec, render_map
from tests.support import geo_scene as gs

VIDEO = "c" * 64
OTHER_VIDEO = "d" * 64
INPUT = "e" * 64


def data(**over: Any) -> GeoSourceData:
    base: dict[str, Any] = {
        "specs": GeoMapSpecs(schema_version="1.0", specs=()),
        "gazetteer": gs.GAZETTEER,
        "claims": gs.CLAIMS,
        "sources": gs.SOURCES,
    }
    base.update(over)
    return GeoSourceData(**base)


SMALL = MapRenderSpec(width=540, height=960, margin_px=32, font_px=30, marker_radius_px=5)


def prepared(**over: Any):  # type: ignore[no-untyped-def]
    d = data(**{k: over.pop(k) for k in ("claims", "sources") if k in over})
    return prepare_map(gs.spec(**over), d, SMALL, None, render_profile_id="shorts_vertical")


def bound(record: GeoMapRecord, video: str = VIDEO, clip: bool = True) -> GeoMapRecord:
    update: dict[str, Any] = {"final_video_sha256": video, "input_hash": INPUT}
    if clip:
        update.update(clip_sha256="f" * 64, clip_duration_ms=4000)
    return GeoMapRecord.model_validate({**record.model_dump(mode="json"), **update})


def test_the_digest_format_matches_the_renderers_manifest_hash() -> None:
    """証跡の manifest_sha256 と描画器の manifest_sha256 は同じ式。食い違えば結びつきが切れる。"""
    manifest = gs.manifest_from(gs.spec())
    assert sha256_of(manifest) == render_map(manifest, SMALL).manifest_sha256


def test_a_passing_request_yields_a_bound_pass_record_and_an_image() -> None:
    p = prepared()
    assert p.applied and p.png is not None
    rec = p.record
    assert rec.verdict is GeoVerdict.PASS
    assert rec.png_sha256 is not None and rec.manifest is not None
    assert rec.manifest_sha256 == sha256_of(rec.manifest)
    assert binding_problems(bound(rec), final_video_sha256=VIDEO) == ()


def test_preparing_twice_gives_identical_records_and_bytes() -> None:
    a, b = prepared(), prepared()
    assert a.png == b.png and a.record == b.record


def test_a_refused_map_is_never_drawn() -> None:
    p = prepared(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))
    assert p.png is None and not p.applied
    assert p.record.png_sha256 is None and p.record.presentation is None
    assert p.record.verdict is not GeoVerdict.PASS


def test_a_map_whose_text_cannot_be_drawn_is_not_applied() -> None:
    """CJK ラベルを ASCII のみの既定フォントで描くと豆腐になる。見た目の BLOCK で使わない。"""
    cjk = (gs.place_req("gz:yashima", "Yashima", label="屋島"),)
    p = prepared(places=cjk)
    assert p.record.verdict is GeoVerdict.BLOCK
    assert not p.applied and p.png is None
    assert p.record.png_sha256 is not None  # 描いた事実は証跡に残る


def test_the_record_refuses_to_pass_without_the_presentation() -> None:
    p = prepared()
    payload = p.record.model_dump(mode="json")
    payload["presentation"] = None
    with pytest.raises(ValueError):
        GeoMapRecord.model_validate(payload)


def test_the_record_refuses_a_verdict_milder_than_its_findings() -> None:
    p = prepared(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),))
    payload = p.record.model_dump(mode="json")
    payload["verdict"] = "pass"
    with pytest.raises(ValueError):
        GeoMapRecord.model_validate(payload)


def test_tampering_with_the_manifest_or_the_verification_breaks_the_binding() -> None:
    rec = bound(prepared().record)
    moved = rec.model_copy(
        update={
            "manifest": rec.manifest.model_copy(update={"as_of": "1600"}),  # type: ignore[union-attr]
        }
    )
    assert BindingProblem.MANIFEST_HASH in binding_problems(moved, final_video_sha256=VIDEO)
    forged = rec.model_copy(update={"verification_sha256": "0" * 64})
    assert BindingProblem.VERIFICATION_HASH in binding_problems(forged, final_video_sha256=VIDEO)


def test_a_pass_record_needs_a_clip_and_the_same_final_video() -> None:
    rec = prepared().record
    assert BindingProblem.NO_CLIP in binding_problems(
        bound(rec, clip=False), final_video_sha256=VIDEO
    )
    assert BindingProblem.NO_FINAL_VIDEO in binding_problems(rec, final_video_sha256=VIDEO)
    assert BindingProblem.OTHER_FINAL_VIDEO in binding_problems(
        bound(rec), final_video_sha256=OTHER_VIDEO
    )


# --- map_quality_checks


def test_an_episode_without_a_map_gets_an_explicit_hash_bound_pass() -> None:
    (check,) = map_quality_checks(final_video_sha256=VIDEO, records=())
    assert check.gate is QualityGate.GEO
    assert check.verdict is QualityVerdict.PASS and check.reason_code is QualityReason.OK
    assert check.evidence_ref == not_used_evidence_ref(VIDEO)
    assert not_used_evidence_ref(VIDEO) != not_used_evidence_ref(OTHER_VIDEO)
    assert check.deterministic and check.scene_id is None


def test_a_verified_and_bound_map_passes_with_the_record_as_evidence() -> None:
    rec = bound(prepared().record)
    (check,) = map_quality_checks(
        final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",)
    )
    assert check.verdict is QualityVerdict.PASS
    assert check.evidence_ref == record_sha256(rec) and check.scene_id == "s2"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"places": (gs.place_req("gz:edo-tokyo", "Tokyo"),)}, QualityReason.GEO_MISMATCH),
        ({"places": (gs.place_req(source_ids=("S-404",)),)}, QualityReason.GEO_MISMATCH),
        ({"as_of": "twelfth century"}, QualityReason.GEO_TIME_BASIS_UNKNOWN),
    ],
)
def test_refused_maps_are_not_passes(over: dict[str, Any], reason: QualityReason) -> None:
    rec = prepared(**over).record
    (check,) = map_quality_checks(
        final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",)
    )
    assert check.verdict is not QualityVerdict.PASS
    assert check.reason_code is reason


def test_a_presentation_only_defect_is_a_visual_defect() -> None:
    rec = prepared(places=(gs.place_req("gz:yashima", "Yashima", label="屋島"),)).record
    (check,) = map_quality_checks(
        final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",)
    )
    assert (
        check.verdict is QualityVerdict.BLOCK and check.reason_code is QualityReason.VISUAL_DEFECT
    )


def test_a_specified_map_with_no_evidence_is_blocked_not_treated_as_not_used() -> None:
    (check,) = map_quality_checks(final_video_sha256=VIDEO, records=(), intended_scene_ids=("s2",))
    assert check.verdict is QualityVerdict.BLOCK and check.reason_code is QualityReason.GEO_MISMATCH
    assert check.scene_id == "s2"


def test_evidence_for_another_final_video_is_blocked() -> None:
    rec = bound(prepared().record, video=OTHER_VIDEO)
    (check,) = map_quality_checks(
        final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",)
    )
    assert check.verdict is QualityVerdict.BLOCK and "other_final_video" in check.detail


def test_two_records_for_one_scene_are_ambiguous() -> None:
    rec = bound(prepared().record)
    checks = map_quality_checks(
        final_video_sha256=VIDEO, records=(rec, rec), intended_scene_ids=("s2",)
    )
    assert [c.verdict for c in checks] == [QualityVerdict.BLOCK]


def test_the_checks_never_echo_input_strings_and_are_deterministic() -> None:
    rec = prepared(places=(gs.place_req("gz:edo-tokyo", "Tokyo"),)).record
    a = map_quality_checks(final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",))
    b = map_quality_checks(final_video_sha256=VIDEO, records=(rec,), intended_scene_ids=("s2",))
    assert a == b
    assert "Tokyo" not in a[0].detail and "edo" not in a[0].detail.lower()


def test_a_malformed_final_video_hash_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        map_quality_checks(final_video_sha256="nope", records=())
