"""YouTube の処理状態の分類（ADR-0022）。純粋関数なので網羅的に検査する。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from domain.upload.ports import VideoProcessingState
from domain.upload.processing import ProcessingOutcome, classify_processing

CHANNEL = "UC" + "a" * 22
OK = VideoProcessingState(
    found=True,
    upload_status="processed",
    processing_status="succeeded",
    failure_reason=None,
    rejection_reason=None,
    privacy_status="private",
    channel_id=CHANNEL,
)


def test_processed_private_own_channel_is_processed() -> None:
    verdict = classify_processing(OK, CHANNEL)
    assert verdict.outcome is ProcessingOutcome.PROCESSED and verdict.reason == "processed"


def test_not_found_is_pending() -> None:
    state = VideoProcessingState(found=False)
    verdict = classify_processing(state, CHANNEL)
    assert verdict.outcome is ProcessingOutcome.PENDING and verdict.reason == "not_found_yet"


@pytest.mark.parametrize(
    ("upload_status", "processing_status"),
    [
        ("uploaded", "processing"),
        ("uploaded", None),
        ("uploaded", "succeeded"),
        (None, "processing"),
        (None, None),
    ],
)
def test_still_uploading_or_processing_is_pending(upload_status, processing_status) -> None:
    state = replace(OK, upload_status=upload_status, processing_status=processing_status)
    assert classify_processing(state, CHANNEL).outcome is ProcessingOutcome.PENDING


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"upload_status": "rejected", "rejection_reason": "duplicate"}, "rejected_duplicate"),
        ({"upload_status": "rejected", "rejection_reason": None}, "rejected_unknown"),
        ({"upload_status": "failed", "failure_reason": "codec"}, "failed_codec"),
        ({"upload_status": "deleted"}, "deleted"),
        (
            {"upload_status": "uploaded", "processing_status": "failed"},
            "processing_failed",
        ),
        (
            {"upload_status": "uploaded", "processing_status": "terminated"},
            "processing_terminated",
        ),
        ({"upload_status": "mystery"}, "unknown_upload_status"),
        ({"channel_id": "UC" + "z" * 22}, "channel_mismatch"),
        ({"channel_id": None}, "channel_mismatch"),
        ({"privacy_status": "public"}, "not_private"),
        ({"privacy_status": "unlisted", "upload_status": "uploaded"}, "not_private"),
        ({"privacy_status": None}, "not_private"),
    ],
)
def test_terminal_problems_are_failed(changes, reason) -> None:
    verdict = classify_processing(replace(OK, **changes), CHANNEL)
    assert verdict.outcome is ProcessingOutcome.FAILED and verdict.reason == reason


def test_channel_mismatch_wins_over_processed() -> None:
    state = replace(OK, channel_id="UC" + "b" * 22)
    assert classify_processing(state, CHANNEL).reason == "channel_mismatch"


def test_reason_codes_are_identifier_safe() -> None:
    state = replace(OK, upload_status="rejected", rejection_reason="x=1&access_token=abc")
    verdict = classify_processing(state, CHANNEL)
    assert verdict.reason == "rejected_unknown" and verdict.reason.isidentifier()
