"""BGM の採否（ADR-0077 / INV-113 / INV-114）。純粋関数。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from domain.render.bgm import BgmSkipReason, resolve_bgm
from domain.render.identity import render_input_hash
from tests.support.bgm_catalog import NOW, make_track, sha
from tests.support.bgm_render import (
    CREDIT_TEXT,
    bgm_plan,
    credit_track,
    cue,
    file_shas,
    sheet,
)
from tests.unit.test_bgm_off_golden import GOLDEN_INPUT_HASH
from tests.unit.test_render_identity import BASE

GOOD = make_track("bgm-approved", "a")


def _resolve(cue_sheet, tracks=(GOOD,), *, profile="shorts_vertical", shas=None, now=NOW):
    return resolve_bgm(
        cue_sheet,
        tracks,
        plan=bgm_plan(profile),
        now=now,
        file_sha256_by_track=file_shas(*tracks) if shas is None else shas,
    )


def test_no_cue_sheet_means_not_applied_and_identity_is_the_pre_bgm_hash() -> None:
    res = _resolve(None)
    assert res.applied is False and res.identity is None
    assert res.rejections[0].reason is BgmSkipReason.NO_CUE_SHEET
    assert render_input_hash(**BASE, bgm=res.identity) == GOLDEN_INPUT_HASH


def test_a_usable_cue_sheet_changes_the_input_hash_and_each_mix_field_moves_it() -> None:
    base = _resolve(sheet())
    assert base.applied and base.identity is not None
    hashes = {render_input_hash(**BASE, bgm=base.identity)}
    assert GOLDEN_INPUT_HASH not in hashes
    for change in (
        {"target_relative_level_db": -20},
        {"fade_in_ms": 100},
        {"end_ms": 1_900},
        {"ducking_policy": "none"},
    ):
        other = _resolve(sheet(cue(**change)))
        assert other.identity is not None
        hashes.add(render_input_hash(**BASE, bgm=other.identity))
    assert len(hashes) == 5


def test_narration_priority_caps_the_level_but_quieter_cues_are_kept() -> None:
    loud = _resolve(sheet(cue(target_relative_level_db=0)))
    quiet = _resolve(sheet(cue(target_relative_level_db=-30)))
    assert loud.plan is not None and quiet.plan is not None
    assert loud.plan.cues[0].level_db == -12
    assert quiet.plan.cues[0].level_db == -30


@pytest.mark.parametrize(
    ("track", "expected"),
    [
        (make_track("bgm-approved", "a", license_status="expired"), "license_not_approved"),
        (
            make_track(
                "bgm-approved",
                "a",
                license_status="unverified",
                license_evidence_sha256=None,
                license_checked_at=None,
            ),
            "license_not_approved",
        ),
        (make_track("bgm-approved", "a", valid_until=NOW), "expired"),
        (make_track("bgm-approved", "a", status="quarantined"), "track_not_active"),
        (make_track("bgm-approved", "a", reuse_allowed=False), "reuse_not_allowed"),
        (make_track("bgm-approved", "a", commercial_use_allowed=False), "commercial_not_allowed"),
        (make_track("bgm-approved", "a", modification_allowed=False), "modification_not_allowed"),
        (make_track("bgm-approved", "a", allowed_formats=("longform",)), "format_not_allowed"),
    ],
)
def test_a_track_that_fails_any_license_fact_is_never_used_even_if_it_is_the_only_one(
    track, expected: str
) -> None:
    res = _resolve(sheet(), (track,))
    assert res.applied is False and res.plan is None and res.identity is None
    rejection = next(r for r in res.rejections if r.reason is BgmSkipReason.LICENSE_REJECTED)
    assert expected in rejection.detail and rejection.track_id == "bgm-approved"


def test_a_track_expiring_after_the_cue_sheet_was_made_is_rejected_at_render_time() -> None:
    track = make_track("bgm-approved", "a", valid_until=NOW + timedelta(hours=1))
    assert _resolve(sheet(), (track,)).applied is True
    later = _resolve(sheet(), (track,), now=NOW + timedelta(hours=1))
    assert later.applied is False


def test_a_shorts_only_restriction_follows_the_profile_format() -> None:
    longform_only = make_track("bgm-approved", "a", allowed_formats=("longform",))
    assert _resolve(sheet(), (longform_only,), profile="shorts_vertical").applied is False
    assert _resolve(sheet(), (longform_only,), profile="long_form_horizontal").applied is True


def test_unknown_track_altered_or_missing_file_and_stale_evidence_are_not_used() -> None:
    unknown = _resolve(sheet(cue(track_id="bgm-nope")))
    assert [r.reason for r in unknown.rejections] == [BgmSkipReason.UNKNOWN_TRACK]
    for shas in ({"bgm-approved": sha("0")}, {"bgm-approved": None}, {}):
        res = _resolve(sheet(), shas=shas)
        assert res.applied is False
        assert BgmSkipReason.FILE_MISSING_OR_ALTERED in {r.reason for r in res.rejections}
    stale = _resolve(sheet(cue(license_evidence_hash=sha("9"))))
    assert stale.applied is False
    assert BgmSkipReason.CREDITS_INVALID in {r.reason for r in stale.rejections}


def test_one_bad_cue_drops_all_of_bgm() -> None:
    good = make_track("bgm-approved", "a")
    bad = make_track("bgm-bad", "b", license_status="revoked")
    cues = sheet(
        cue(),
        cue(
            track_id="bgm-bad", source_sha256=sha("b"), scene_id="s2", start_ms=2_000, end_ms=4_000
        ),
    )
    res = _resolve(cues, (good, bad))
    assert res.applied is False and res.plan is None


def test_cue_outside_the_timeline_or_for_an_unknown_scene_is_not_used() -> None:
    assert _resolve(sheet(cue(end_ms=4_001))).applied is False
    assert _resolve(sheet(cue(scene_id="s9"))).applied is False
    assert _resolve(sheet(cue(end_ms=4_000))).applied is True


def test_an_unknown_render_profile_never_uses_bgm() -> None:
    plan = bgm_plan()
    odd = plan.model_copy(update={"profile": plan.profile.model_copy(update={"profile_id": "odd"})})
    res = resolve_bgm(sheet(), (GOOD,), plan=odd, now=NOW, file_sha256_by_track=file_shas(GOOD))
    assert [r.reason for r in res.rejections] == [BgmSkipReason.UNKNOWN_PROFILE_FORMAT]


def test_credit_lines_are_the_catalog_text_exactly() -> None:
    track = credit_track()
    res = _resolve(
        sheet(cue(track_id="bgm-credit", source_sha256=sha("f"), credits_text=CREDIT_TEXT)),
        (track,),
    )
    assert res.applied and res.credit_lines == (CREDIT_TEXT,)
    missing = _resolve(sheet(cue(track_id="bgm-credit", source_sha256=sha("f"))), (track,))
    assert missing.applied is False


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        _resolve(sheet(), now=NOW.replace(tzinfo=None))
