"""domain/music/selection.py（INV-97、設計 v3 T12/T13）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from contracts.music import BgmTrack
from domain.music import RejectReason, select_approved
from tests.support.bgm_catalog import NOW, catalog, make_track


def select(tracks: tuple[BgmTrack, ...], **overrides: Any):
    args: dict[str, Any] = {
        "mood": "mysterious",
        "fmt": "shorts",
        "platform": "youtube",
        "duration_ms": 60_000,
        "now": NOW,
        "recent_usage": {},
    }
    args.update(overrides)
    return select_approved(tracks, **args)


def reasons(result: Any) -> dict[str, set[RejectReason]]:
    return {r.track_id: set(r.reasons) for r in result.rejected}


def test_only_legal_tracks_are_candidates_for_shorts() -> None:
    result = select(catalog())
    assert {t.track_id for t in result.candidates} == {"bgm-approved", "bgm-credit"}
    got = reasons(result)
    assert got["bgm-unverified"] == {RejectReason.LICENSE_NOT_APPROVED}
    assert got["bgm-expired"] == {RejectReason.EXPIRED}
    assert got["bgm-longform-only"] == {RejectReason.FORMAT_NOT_ALLOWED}


def test_longform_only_track_is_allowed_for_longform() -> None:
    result = select(catalog(), fmt="longform")
    assert "bgm-longform-only" in {t.track_id for t in result.candidates}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"license_status": "revoked"}, RejectReason.LICENSE_NOT_APPROVED),
        ({"license_status": "expired"}, RejectReason.LICENSE_NOT_APPROVED),
        ({"status": "quarantined"}, RejectReason.TRACK_NOT_ACTIVE),
        ({"status": "retired"}, RejectReason.TRACK_NOT_ACTIVE),
        ({"reuse_allowed": False}, RejectReason.REUSE_NOT_ALLOWED),
        ({"commercial_use_allowed": False}, RejectReason.COMMERCIAL_NOT_ALLOWED),
        ({"modification_allowed": False}, RejectReason.MODIFICATION_NOT_ALLOWED),
        ({"allowed_platforms": ()}, RejectReason.PLATFORM_NOT_ALLOWED),
        ({"allowed_formats": ("longform",)}, RejectReason.FORMAT_NOT_ALLOWED),
    ],
)
def test_each_license_fact_excludes_the_only_track(
    overrides: dict[str, Any], reason: RejectReason
) -> None:
    result = select((make_track(**overrides),))
    assert result.candidates == ()
    assert reason in result.rejected[0].reasons


def test_valid_until_boundary_is_exclusive_of_now() -> None:
    just_before = make_track(valid_until=NOW + timedelta(seconds=1))
    at_now = make_track("bgm-at-now", "b", valid_until=NOW)
    result = select((just_before, at_now))
    assert [t.track_id for t in result.candidates] == ["bgm-approved"]
    assert reasons(result)["bgm-at-now"] == {RejectReason.EXPIRED}


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        select(catalog(), now=datetime(2026, 11, 1))


def test_attribution_required_without_text_is_excluded_even_if_contract_is_bypassed() -> None:
    bad = BgmTrack.model_construct(
        **{
            **make_track().model_dump(),
            "attribution_required": True,
            "exact_attribution_text": None,
        }
    )
    result = select((bad,))
    assert result.candidates == ()
    assert RejectReason.ATTRIBUTION_TEXT_MISSING in result.rejected[0].reasons


def test_multiple_reasons_are_all_reported() -> None:
    result = select(
        (make_track(license_status="revoked", status="retired", allowed_formats=("longform",)),)
    )
    assert set(result.rejected[0].reasons) == {
        RejectReason.LICENSE_NOT_APPROVED,
        RejectReason.TRACK_NOT_ACTIVE,
        RejectReason.FORMAT_NOT_ALLOWED,
    }


def test_recent_use_is_soft_preference_only() -> None:
    a = make_track("bgm-a", "a")
    b = make_track("bgm-b", "b")
    result = select((a, b), recent_usage={"bgm-a": 3})
    assert [t.track_id for t in result.candidates] == ["bgm-b", "bgm-a"]


def test_recent_use_never_excludes_the_only_legal_track() -> None:
    legal = make_track("bgm-a", "a")
    illegal = make_track(
        "bgm-b",
        "b",
        license_status="unverified",
        license_evidence_sha256=None,
        license_checked_at=None,
    )
    result = select((legal, illegal), recent_usage={"bgm-a": 99})
    assert [t.track_id for t in result.candidates] == ["bgm-a"]


def test_recent_use_never_promotes_an_illegal_track() -> None:
    legal = make_track("bgm-a", "a")
    illegal = make_track("bgm-b", "b", status="retired")
    result = select((legal, illegal), recent_usage={"bgm-a": 99, "bgm-b": 0})
    assert [t.track_id for t in result.candidates] == ["bgm-a"]


def test_mood_is_a_soft_preference_and_does_not_exclude() -> None:
    calm = make_track("bgm-calm", "a", mood_tags=("calm",))
    mysterious = make_track("bgm-myst", "b", mood_tags=("mysterious",))
    result = select((calm, mysterious))
    assert [t.track_id for t in result.candidates] == ["bgm-myst", "bgm-calm"]
    only_calm = select((calm,))
    assert [t.track_id for t in only_calm.candidates] == ["bgm-calm"]


def test_ordering_is_deterministic_regardless_of_input_order() -> None:
    tracks = tuple(make_track(f"bgm-{c}", c) for c in "abcd")
    forward = select(tracks)
    backward = select(tuple(reversed(tracks)))
    assert forward.candidates == backward.candidates


def test_empty_catalog_gives_no_candidates_so_caller_can_request_acquisition() -> None:
    result = select(())
    assert result.candidates == () and result.rejected == ()


def test_now_is_not_read_from_the_clock() -> None:
    far_future = datetime(2100, 1, 1, tzinfo=UTC)
    result = select((make_track(valid_until=NOW + timedelta(days=1)),), now=far_future)
    assert result.candidates == ()
