"""BGM の品質検査（ADR-0083 §8、INV-138）。純粋関数。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from contracts.music_render import (
    BgmEvidenceTrack,
    BgmRenderEvidence,
    LoudnessMeasurement,
    SpeechIntelligibility,
    SpeechMarginScene,
)
from contracts.quality import QualityGate, QualityReason, QualityVerdict
from domain.music import quality_checks as qc
from domain.music.quality_checks import bgm_quality_checks
from domain.render.loudness import SILENT_INTEGRATED_CENTI, evaluate_loudness
from tests.support.bgm_catalog import NOW, make_track, sha
from tests.support.bgm_render import CREDIT_TEXT, credit_track

EPISODE = "11111111-2222-4333-8444-555555555555"
VIDEO = sha("1")
EVIDENCE_SHA = sha("2")
GOOD = LoudnessMeasurement(integrated_lufs_centi=-1500, true_peak_dbtp_centi=-300, lra_centi=500)
TRACK = make_track("bgm-approved", "a")


def _evidence(
    *, tracks=(TRACK,), credits=(), video=VIDEO, measured=GOOD, license_hash=None
) -> BgmRenderEvidence:
    return BgmRenderEvidence(
        episode_id=EPISODE,
        input_hash=sha("3"),
        final_video_sha256=video,
        mix_plan_sha256=sha("4"),
        tracks=tuple(
            BgmEvidenceTrack(
                track_id=t.track_id,
                source_sha256=t.file_sha256,
                license_evidence_hash=license_hash or t.license_evidence_sha256 or sha("e"),
            )
            for t in tracks
        ),
        credit_lines=credits,
        loudness=evaluate_loudness(measured),
    )


def _speech(passed: bool = True) -> SpeechIntelligibility:
    return SpeechIntelligibility(
        scenes=(
            SpeechMarginScene(
                scene_id="s1", voice_lufs_centi=-1600, music_lufs_centi=-3000, margin_centi_lu=1400
            ),
        ),
        min_margin_centi_lu=1400 if passed else 300,
        threshold_centi_lu=1200,
        passed=passed,
    )


def _run(
    evidence: BgmRenderEvidence | None,
    *,
    catalog: Any = (TRACK,),
    intelligibility: Any = "ok",
    now: datetime = NOW,
    fmt: Any = "shorts",
):
    return bgm_quality_checks(
        final_video_sha256=VIDEO,
        evidence=evidence,
        evidence_sha256=None if evidence is None else EVIDENCE_SHA,
        catalog=catalog,
        now=now,
        intelligibility=_speech() if intelligibility == "ok" else intelligibility,
        video_format=fmt,
    )


def _verdicts(checks):
    return {(c.gate, c.verdict) for c in checks}


def test_not_used_is_an_explicit_pass_bound_to_the_final_video_hash_for_both_gates() -> None:
    checks = _run(None)
    assert {c.gate for c in checks} == {QualityGate.MUSIC, QualityGate.LICENSE}
    for c in checks:
        assert c.verdict is QualityVerdict.PASS and c.reason_code is QualityReason.OK
        assert c.evidence_ref == VIDEO and c.deterministic


def test_a_clean_bgm_video_passes_every_check_and_each_is_bound_to_the_evidence() -> None:
    checks = _run(_evidence())
    assert checks and all(c.verdict is QualityVerdict.PASS for c in checks)
    assert {c.evidence_ref for c in checks} == {EVIDENCE_SHA}
    assert {c.gate for c in checks} == {QualityGate.MUSIC, QualityGate.LICENSE}


def test_credit_lines_must_equal_the_catalog_text() -> None:
    credit = credit_track()
    good = _run(_evidence(tracks=(credit,), credits=(CREDIT_TEXT,)), catalog=(credit,))
    assert all(c.verdict is QualityVerdict.PASS for c in good)
    for wrong in ((), (CREDIT_TEXT + "!",), (CREDIT_TEXT, CREDIT_TEXT)):
        checks = _run(_evidence(tracks=(credit,), credits=wrong), catalog=(credit,))
        assert any(
            c.verdict is QualityVerdict.BLOCK and c.gate is QualityGate.LICENSE for c in checks
        )


def test_evidence_of_another_video_is_blocked_on_both_gates() -> None:
    checks = _run(_evidence(video=sha("9")))
    assert _verdicts(checks) == {
        (QualityGate.MUSIC, QualityVerdict.BLOCK),
        (QualityGate.LICENSE, QualityVerdict.BLOCK),
    }
    assert {c.reason_code for c in checks} == {
        QualityReason.AUDIO_DEFECT,
        QualityReason.LICENSE_UNVERIFIED,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        {"license_status": "revoked"},
        {"status": "quarantined"},
        {"valid_until": NOW - timedelta(seconds=1)},  # 描画後に失効
        {"allowed_formats": ("longform",)},  # Shorts 不可
        {"reuse_allowed": False},
        {"file_sha256": sha("f")},  # カタログの音源が差し替わった
        {"license_evidence_sha256": sha("d")},  # 証拠が更新された（証跡の hash と不一致）
    ],
)
def test_a_track_no_longer_valid_or_changed_since_rendering_is_blocked(mutate) -> None:
    changed = make_track("bgm-approved", "a", **mutate)
    checks = _run(_evidence(), catalog=(changed,))
    assert any(
        c.gate is QualityGate.LICENSE
        and c.verdict is QualityVerdict.BLOCK
        and c.reason_code is QualityReason.LICENSE_UNVERIFIED
        for c in checks
    )


def test_a_track_missing_from_the_catalog_is_blocked() -> None:
    checks = _run(_evidence(), catalog=())
    assert any(c.gate is QualityGate.LICENSE and c.verdict is QualityVerdict.BLOCK for c in checks)


@pytest.mark.parametrize(
    "measured",
    [
        LoudnessMeasurement(integrated_lufs_centi=-600, true_peak_dbtp_centi=-300, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-3000, true_peak_dbtp_centi=-300, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-1400, true_peak_dbtp_centi=0, lra_centi=1),
        LoudnessMeasurement(integrated_lufs_centi=-7000, true_peak_dbtp_centi=None, lra_centi=0),
    ],
)
def test_out_of_range_loudness_in_the_evidence_is_blocked_on_the_music_gate(measured) -> None:
    checks = _run(_evidence(measured=measured))
    assert any(
        c.gate is QualityGate.MUSIC
        and c.verdict is QualityVerdict.BLOCK
        and c.reason_code is QualityReason.AUDIO_DEFECT
        for c in checks
    )


def test_the_check_recomputes_loudness_from_numbers_and_does_not_trust_the_stored_flag() -> None:
    evidence = _evidence(measured=GOOD)
    bad = evidence.loudness.model_copy(update={"passed": True, "reason_code": "ok"})
    bad = bad.model_copy(
        update={
            "measured": LoudnessMeasurement(
                integrated_lufs_centi=-600, true_peak_dbtp_centi=-300, lra_centi=1
            )
        }  # fmt: skip
    )
    forged = evidence.model_copy(update={"loudness": bad})
    assert any(c.verdict is QualityVerdict.BLOCK for c in _run(forged))


def test_the_local_loudness_rule_agrees_with_evaluate_loudness_everywhere() -> None:
    """domain/music は domain/render を import できないので判定を持つ。食い違いはここで落とす。"""
    assert qc._SILENT_INTEGRATED_CENTI == SILENT_INTEGRATED_CENTI
    for integrated in (None, -7000, -6999, -2500, -2400, -1500, -900, -899, 0):
        for peak in (None, -300, -101, -100, -99, 0):
            m = LoudnessMeasurement(
                integrated_lufs_centi=integrated, true_peak_dbtp_centi=peak, lra_centi=0
            )
            assert qc._loudness_in_range(m) == evaluate_loudness(m).passed, (integrated, peak)


def test_speech_margin_unmeasured_is_review_and_too_small_is_block() -> None:
    unmeasured = _run(_evidence(), intelligibility=None)
    assert any(
        c.gate is QualityGate.MUSIC and c.verdict is QualityVerdict.REVIEW for c in unmeasured
    )
    assert not any(c.verdict is QualityVerdict.BLOCK for c in unmeasured)
    failed = _run(_evidence(), intelligibility=_speech(False))
    assert any(c.gate is QualityGate.MUSIC and c.verdict is QualityVerdict.BLOCK for c in failed)


def test_details_are_fixed_ascii_and_never_echo_ids_or_text() -> None:
    credit = credit_track()
    for checks in (
        _run(None),
        _run(_evidence(tracks=(credit,), credits=("x",)), catalog=(credit,)),
    ):
        for c in checks:
            assert c.detail.isascii() and "bgm-" not in c.detail and CREDIT_TEXT not in c.detail


def test_naive_now_and_missing_evidence_hash_are_refused() -> None:
    with pytest.raises(ValueError):
        _run(_evidence(), now=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        bgm_quality_checks(
            final_video_sha256=VIDEO, evidence=_evidence(), evidence_sha256=None, catalog=(TRACK,),
            now=NOW, intelligibility=_speech(), video_format="shorts",
        )  # fmt: skip
