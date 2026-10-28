"""domain/music/ingest.py（INV-99、設計 v3 T12 / T27）。"""

from __future__ import annotations

from domain.music import IngestOutcome, IngestRejectReason, ingest_check
from tests.support.bgm_catalog import catalog, make_track, sha


def test_new_track_with_matching_sha_is_accepted() -> None:
    new = make_track("bgm-new", "1")
    decision = ingest_check(catalog(), new, sha("1"))
    assert decision.outcome is IngestOutcome.ACCEPT


def test_sha_mismatch_is_rejected_as_tampered() -> None:
    new = make_track("bgm-new", "1")
    decision = ingest_check(catalog(), new, sha("2"))
    assert decision.outcome is IngestOutcome.REJECT
    assert decision.reason is IngestRejectReason.SHA_MISMATCH


def test_malformed_actual_sha_is_rejected() -> None:
    new = make_track("bgm-new", "1")
    decision = ingest_check(catalog(), new, "NOT-A-SHA")
    assert decision.outcome is IngestOutcome.REJECT
    assert decision.reason is IngestRejectReason.ACTUAL_SHA_MALFORMED


def test_same_file_is_deduplicated_to_the_existing_track() -> None:
    existing = catalog()[0]
    again = make_track("bgm-reupload", "a")  # 同じ sha、別 track_id
    decision = ingest_check(catalog(), again, existing.file_sha256)
    assert decision.outcome is IngestOutcome.DUPLICATE
    assert decision.existing_track_id == existing.track_id


def test_duplicate_never_upgrades_the_existing_license_facts() -> None:
    """再取込の申告で unverified の既存曲が approved にならない（既存を正とする）。"""
    existing = catalog()[1]  # unverified
    again = make_track("bgm-claims-approved", "b")
    decision = ingest_check(catalog(), again, existing.file_sha256)
    assert decision.outcome is IngestOutcome.DUPLICATE
    assert decision.existing_track_id == "bgm-unverified"


def test_same_track_id_with_different_bytes_is_rejected() -> None:
    swapped = make_track("bgm-approved", "9")  # id は既存、中身が違う
    decision = ingest_check(catalog(), swapped, sha("9"))
    assert decision.outcome is IngestOutcome.REJECT
    assert decision.reason is IngestRejectReason.TRACK_ID_CONFLICT


def test_object_key_must_be_content_addressed_by_the_sha() -> None:
    new = make_track("bgm-new", "1", object_key=f"music/originals/{sha('2')}.mp3")
    decision = ingest_check(catalog(), new, sha("1"))
    assert decision.outcome is IngestOutcome.REJECT
    assert decision.reason is IngestRejectReason.OBJECT_KEY_NOT_CONTENT_ADDRESSED


def test_ingest_check_does_not_mutate_the_catalog() -> None:
    before = catalog()
    ingest_check(before, make_track("bgm-new", "1"), sha("1"))
    assert before == catalog()
