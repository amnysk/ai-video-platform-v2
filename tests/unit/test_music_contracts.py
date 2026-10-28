"""contracts/music.py の検証（ADR-0072）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.music import BgmTrack, BgmUsage, MusicCue, MusicCueSheet
from tests.support.bgm_catalog import make_track, sha


def cue(**overrides: Any) -> MusicCue:
    base: dict[str, Any] = {
        "scene_id": "s1",
        "track_id": "bgm-approved",
        "source_sha256": sha("a"),
        "start_ms": 0,
        "end_ms": 10_000,
        "fade_in_ms": 500,
        "fade_out_ms": 500,
        "target_relative_level_db": -18,
        "ducking_policy": "duck_under_voice",
        "license_evidence_hash": sha("e"),
        "credits_text": None,
    }
    base.update(overrides)
    return MusicCue(**base)


def test_valid_cue_and_sheet() -> None:
    sheet = MusicCueSheet(cues=(cue(),))
    assert sheet.cues[0].target_relative_level_db == -18


@pytest.mark.parametrize(
    "overrides",
    [
        {"end_ms": 0},
        {"end_ms": -1, "start_ms": -5},
        {"fade_in_ms": 9_000, "fade_out_ms": 2_000},
        {"fade_in_ms": -1},
        {"source_sha256": "A" * 64},
        {"source_sha256": "abc"},
        {"license_evidence_hash": "z" * 64},
        {"target_relative_level_db": -18.5},
        {"target_relative_level_db": 3},
        {"ducking_policy": "loud"},
        {"scene_id": "scene-1"},
    ],
)
def test_invalid_cue_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        cue(**overrides)


def test_level_is_integer_so_hash_is_stable() -> None:
    assert isinstance(cue().target_relative_level_db, int)
    with pytest.raises(ValidationError):
        cue(target_relative_level_db="-18")  # strict: 文字列・float を黙って変換しない


def test_sheet_needs_at_least_one_cue_and_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        MusicCueSheet(cues=())
    with pytest.raises(ValidationError):
        MusicCueSheet(cues=(cue(),), unknown=1)  # type: ignore[call-arg]


def test_attribution_text_is_required_iff_attribution_required() -> None:
    with pytest.raises(ValidationError):
        make_track(attribution_required=True, exact_attribution_text=None)
    with pytest.raises(ValidationError):
        make_track(attribution_required=True, exact_attribution_text="   ")
    with pytest.raises(ValidationError):
        make_track(attribution_required=False, exact_attribution_text="Music by X")
    assert make_track(attribution_required=True, exact_attribution_text="Music by X")


def test_approved_requires_evidence_and_check_time() -> None:
    with pytest.raises(ValidationError):
        make_track(license_evidence_sha256=None)
    with pytest.raises(ValidationError):
        make_track(license_checked_at=None)
    assert make_track(license_status="unverified", license_evidence_sha256=None)


@pytest.mark.parametrize(
    "overrides",
    [
        {"file_sha256": "G" * 64},
        {"license_status": "maybe"},
        {"status": "gone"},
        {"allowed_formats": ("tiktok",)},
        {"duration_ms": 0},
        {"track_id": "x"},
        {"valid_until": datetime(2027, 1, 1)},  # naive は期限比較を曖昧にする
        {"object_key": "/abs/path.mp3"},
    ],
)
def test_invalid_track_is_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make_track(**overrides)


def test_track_is_frozen() -> None:
    track: BgmTrack = make_track()
    with pytest.raises(ValidationError):
        track.status = "retired"  # type: ignore[misc]


def test_usage_record() -> None:
    usage = BgmUsage(
        episode_id="11111111-1111-4111-8111-111111111111",
        track_id="bgm-approved",
        license_evidence_hash=sha("e"),
        final_video_sha256=sha("9"),
    )
    assert usage.final_video_sha256 == sha("9")
    with pytest.raises(ValidationError):
        BgmUsage(
            episode_id="not-a-uuid",
            track_id="bgm-approved",
            license_evidence_hash=sha("e"),
            final_video_sha256=sha("9"),
        )


@pytest.mark.parametrize("text", ["Music <by> X", "a > b", "<", "x<y"])
def test_attribution_text_with_chars_the_description_sanitizer_rewrites_is_rejected(
    text: str,
) -> None:
    """sanitize_youtube_text が < > を書き換え、指定文が原文のまま載らなくなる（INV-98）。"""
    with pytest.raises(ValidationError):
        make_track(attribution_required=True, exact_attribution_text=text)


@pytest.mark.parametrize(
    "blank", ["\u200b", " \u200b\u200d\ufeff ", "\u00a0\u3000", "\u2060", "\ufeff\t\n"]
)
def test_attribution_text_blank_after_removing_whitespace_and_format_chars_is_rejected(
    blank: str,
) -> None:
    """ゼロ幅文字だけの指定文は strip() では空にならず、見えないクレジットが通ってしまう。"""
    with pytest.raises(ValidationError):
        make_track(attribution_required=True, exact_attribution_text=blank)


def test_attribution_text_blank_check_uses_nfkc() -> None:
    with pytest.raises(ValidationError):
        make_track(attribution_required=True, exact_attribution_text="\u2003\u200b")


def test_cue_sheet_rejects_overlapping_cues_in_the_same_scene() -> None:
    with pytest.raises(ValidationError):
        MusicCueSheet(cues=(cue(start_ms=0, end_ms=10_000), cue(start_ms=5_000, end_ms=15_000)))


def test_cue_sheet_rejects_duplicate_cues() -> None:
    with pytest.raises(ValidationError):
        MusicCueSheet(cues=(cue(), cue()))


def test_cue_sheet_rejects_non_monotonic_order() -> None:
    later = cue(scene_id="s2", start_ms=10_000, end_ms=20_000)
    earlier = cue(scene_id="s1", start_ms=0, end_ms=10_000)
    with pytest.raises(ValidationError):
        MusicCueSheet(cues=(later, earlier))


def test_cue_sheet_allows_adjacent_cues_and_cross_scene_overlap_is_not_a_same_scene_overlap() -> (
    None
):
    a = cue(scene_id="s1", start_ms=0, end_ms=10_000)
    b = cue(scene_id="s1", start_ms=10_000, end_ms=20_000)
    assert MusicCueSheet(cues=(a, b))
