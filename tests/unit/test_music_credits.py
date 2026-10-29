"""domain/music/credits.py（INV-98、設計 v3 T14）。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest

from contracts.music import BgmTrack, MusicCue, MusicCueSheet
from contracts.upload import YOUTUBE_DESCRIPTION_MAX_BYTES, sanitize_youtube_text
from domain.music import CreditsError, credits_for, fit_credits
from tests.support.bgm_catalog import NOW, catalog, make_track, sha

CREDIT = 'Music: "Fixture Song" by Fixture Artist (CC BY 4.0)'


def make_cue(
    track_id: str,
    sha_char: str,
    scene: str = "s1",
    credits: str | None = None,
    evidence: str = "e",
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
        license_evidence_hash=sha(evidence),
        credits_text=credits,
    )


def test_credit_required_track_yields_exact_text() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits=CREDIT),))
    assert credits_for(sheet, catalog(), now=NOW) == (CREDIT,)


def test_no_credit_for_tracks_that_do_not_need_it() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "a"),))
    assert credits_for(sheet, catalog(), now=NOW) == ()


def test_same_track_in_many_cues_is_credited_once_in_first_use_order() -> None:
    sheet = MusicCueSheet(
        cues=(
            make_cue("bgm-credit", "f", "s1", CREDIT),
            make_cue("bgm-approved", "a", "s2"),
            make_cue("bgm-credit", "f", "s3", CREDIT),
        )
    )
    assert credits_for(sheet, catalog(), now=NOW) == (CREDIT,)


def test_cue_for_a_track_missing_from_the_catalog_is_an_error() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-ghost", "a"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog(), now=NOW)


def test_stale_or_altered_credit_copy_in_cue_is_an_error_not_silently_replaced() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits="Music by someone"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog(), now=NOW)


def test_missing_credit_copy_in_cue_for_required_track_is_an_error() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits=None),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog(), now=NOW)


def test_source_sha_must_match_the_catalog_file() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "0"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog(), now=NOW)


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


def test_fitted_credits_are_byte_identical_to_what_is_sent() -> None:
    """fit が測った文字列 = 説明欄へ実際に送る文字列（sanitize 後）。"""
    credits = (CREDIT, "Music: Other by Someone")
    fit = fit_credits(YOUTUBE_DESCRIPTION_MAX_BYTES, credits)
    sent = sanitize_youtube_text("\n".join(credits))
    assert fit.rendered == sent
    assert fit.required_bytes == len(sent.encode("utf-8"))


def test_fit_measures_after_sanitization_not_raw_bytes() -> None:
    raw = "<<<"  # 生は 3 bytes、全角化後は 9 bytes
    assert not fit_credits(3, (raw,)).ok
    assert fit_credits(9, (raw,)).ok


def test_fit_uses_the_shared_description_limit_not_a_literal() -> None:
    at_limit = "A" * YOUTUBE_DESCRIPTION_MAX_BYTES
    assert fit_credits(YOUTUBE_DESCRIPTION_MAX_BYTES, (at_limit,)).ok
    assert not fit_credits(YOUTUBE_DESCRIPTION_MAX_BYTES, (at_limit + "A",)).ok


def test_cue_license_evidence_hash_must_match_the_catalog() -> None:
    """cue が別のライセンス証拠を指している（差替え・古い版）のに通ること。"""
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "a", evidence="1"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, catalog(), now=NOW)


@pytest.mark.parametrize(
    "overrides",
    [
        {"license_status": "revoked"},
        {"license_status": "expired"},
        {"status": "quarantined"},
        {"valid_until": NOW - timedelta(seconds=1)},
        {"valid_until": NOW},
        {"reuse_allowed": False},
        {"commercial_use_allowed": False},
        {"license_checked_at": NOW + timedelta(days=1)},
    ],
)
def test_track_that_stopped_being_usable_after_selection_is_rejected(
    overrides: dict[str, Any],
) -> None:
    """選曲後に取消・失効・期限切れ・隔離された曲を、cue sheet の再検査で止める。"""
    track = make_track(**overrides)
    sheet = MusicCueSheet(cues=(make_cue(track.track_id, "a"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, (track,), now=NOW)


def test_naive_now_is_refused_by_credits_for() -> None:
    sheet = MusicCueSheet(cues=(make_cue("bgm-approved", "a"),))
    with pytest.raises(ValueError):
        credits_for(sheet, catalog(), now=datetime(2026, 11, 1))


def test_credit_text_that_would_be_rewritten_on_send_is_an_error_even_if_contract_is_bypassed() -> (
    None
):
    bad = BgmTrack.model_construct(
        **{
            **make_track("bgm-credit", "f").model_dump(),
            "attribution_required": True,
            "exact_attribution_text": "Music <x>",
        }
    )
    sheet = MusicCueSheet(cues=(make_cue("bgm-credit", "f", credits="Music <x>"),))
    with pytest.raises(CreditsError):
        credits_for(sheet, (bad,), now=NOW)
