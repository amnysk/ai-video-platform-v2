"""domain/music/credits.py（INV-98、設計 v3 T14）。"""

from __future__ import annotations

import pytest

from contracts.music import MusicCue, MusicCueSheet
from domain.music import CreditsError, credits_for, fit_credits
from tests.support.bgm_catalog import catalog, sha

CREDIT = 'Music: "Fixture Song" by Fixture Artist (CC BY 4.0)'


def make_cue(
    track_id: str, sha_char: str, scene: str = "s1", credits: str | None = None
) -> MusicCue:
    return MusicCue(
        scene_id=scene,
        track_id=track_id,
        source_sha256=sha(sha_char),
        start_ms=0,
        end_ms=5_000,
        fade_in_ms=0,
        fade_out_ms=0,
        target_relative_level_db=-18,
        ducking_policy="duck_under_voice",
        license_evidence_hash=sha("e"),
        credits_text=credits,
    )


def test_credit_required_track_yields_exact_text() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits=CREDIT),))
    assert credits_for(sheet, catalog()) == (CREDIT,)


def test_no_credit_for_tracks_that_do_not_need_it() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "a"),))
    assert credits_for(sheet, catalog()) == ()


def test_same_track_in_many_cues_is_credited_once_in_first_use_order() -> None:
    sheet = MusicCueSheet(
        cues=(
            make_cue("bgm-credit", "f", "s1", CREDIT),
            make_cue("bgm-approved", "a", "s2"),
            make_cue("bgm-credit", "f", "s3", CREDIT),
        )
    )
    assert credits_for(sheet, catalog()) == (CREDIT,)


def test_cue_for_a_track_missing_from_the_catalog_is_an_error() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-ghost", "a"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog())


def test_stale_or_altered_credit_copy_in_cue_is_an_error_not_silently_replaced() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits="Music by someone"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog())


def test_missing_credit_copy_in_cue_for_required_track_is_an_error() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits=None),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog())


def test_source_sha_must_match_the_catalog_file() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "0"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog())


def test_fit_credits_ok_when_within_budget() -> None:
    fit = fit_credits(1000, (CREDIT,))
    assert fit.ok and fit.credits == (CREDIT,)


def test_fit_credits_blocks_instead_of_truncating() -> None:
    needed = len(CREDIT.encode("utf-8"))
    fit = fit_credits(needed - 1, (CREDIT,))
    assert not fit.ok
    assert fit.credits == (CREDIT,)  # 切り詰めた文字列を返さない
    assert fit.required_bytes == needed and fit.budget_bytes == needed - 1


def test_fit_credits_counts_utf8_bytes_not_characters() -> None:
    jp = "音楽: 「テスト曲」 作曲者テスト (CC BY 4.0)"
    assert len(jp) < len(jp.encode("utf-8"))
    assert not fit_credits(len(jp), (jp,)).ok
    assert fit_credits(len(jp.encode("utf-8")), (jp,)).ok


def test_fit_credits_counts_the_newline_separators() -> None:
    a, b = "A" * 10, "B" * 10
    assert fit_credits(21, (a, b)).ok
    assert not fit_credits(20, (a, b)).ok


def test_empty_credits_always_fit_even_with_zero_budget() -> None:
    assert fit_credits(0, ()).ok


def test_negative_budget_blocks() -> None:
    assert not fit_credits(-1, (CREDIT,)).ok
