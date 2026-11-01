"""承認済み BGM の保存と再利用（ADR-0083 §2、INV-133）。一時ディレクトリだけを使う。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from contracts.music import BgmTrack
from domain.music.ingest import IngestOutcome, IngestRejectReason
from domain.music.placement import plan_cue_sheet
from infrastructure.music.catalog_store import (
    CatalogStoreError,
    read_catalog,
    register_track,
    write_cue_sheet,
)
from infrastructure.render.bgm_source import LocalDirectoryBgmSource
from tests.support.bgm_catalog import make_track
from tests.support.bgm_render import placement_plan

AUDIO = b"RIFF fixture audio bytes"
EPISODE = "11111111-2222-4333-8444-555555555555"


def _track(audio: bytes = AUDIO, track_id: str = "bgm-fixture", **kw) -> BgmTrack:
    sha = hashlib.sha256(audio).hexdigest()
    return make_track(
        track_id, "a", file_sha256=sha, object_key=f"music/originals/{sha}.wav",
        loudness_lufs=-18.0, **kw,
    )  # fmt: skip


def test_registering_writes_the_content_addressed_original_and_the_catalog(tmp_path: Path) -> None:
    track = _track()
    decision = register_track(tmp_path, track, AUDIO)
    assert decision.outcome is IngestOutcome.ACCEPT
    assert (tmp_path / track.object_key).read_bytes() == AUDIO
    assert read_catalog(tmp_path) == (track,)


def test_the_same_bytes_are_reused_not_registered_again(tmp_path: Path) -> None:
    """別 Episode が同じ曲を使っても再取込しない。既存の承認事実は再登録の申告で変わらない。"""
    first = _track()
    register_track(tmp_path, first, AUDIO)
    other_claim = _track(track_id="bgm-same-bytes-other-name", license_status="revoked")
    decision = register_track(tmp_path, other_claim, AUDIO)
    assert decision.outcome is IngestOutcome.DUPLICATE
    assert decision.existing_track_id == "bgm-fixture"
    assert read_catalog(tmp_path) == (first,)  # 承認のまま、増えていない


def test_a_lying_sha_or_a_swapped_track_id_is_rejected_and_nothing_is_written(
    tmp_path: Path,
) -> None:
    track = _track()
    bad = register_track(tmp_path, track, b"different bytes")
    assert bad.outcome is IngestOutcome.REJECT and bad.reason is IngestRejectReason.SHA_MISMATCH
    assert read_catalog(tmp_path) == () and not (tmp_path / "music").exists()

    register_track(tmp_path, track, AUDIO)
    other = b"other audio"
    conflict = register_track(tmp_path, _track(other, track_id="bgm-fixture"), other)
    assert conflict.reason is IngestRejectReason.TRACK_ID_CONFLICT
    assert read_catalog(tmp_path) == (track,)


def test_a_tampered_original_is_not_silently_accepted(tmp_path: Path) -> None:
    track = _track()
    original = tmp_path / track.object_key
    original.parent.mkdir(parents=True)
    original.write_bytes(b"tampered")
    with pytest.raises(CatalogStoreError):
        register_track(tmp_path, track, AUDIO)
    assert read_catalog(tmp_path) == ()


def test_a_corrupt_catalog_stops_registration_instead_of_overwriting(tmp_path: Path) -> None:
    (tmp_path / "catalog.json").write_text("{not json")
    with pytest.raises(CatalogStoreError):
        register_track(tmp_path, _track(), AUDIO)
    assert (tmp_path / "catalog.json").read_text() == "{not json"


def test_what_is_written_is_what_the_render_source_reads_back(tmp_path: Path) -> None:
    """登録 → 配置 → cue sheet 保存 → 描画側の供給元が同じ内容を読む（書き手と読み手の一致）。"""
    track = _track()
    register_track(tmp_path, track, AUDIO)
    sheet = plan_cue_sheet(placement_plan(), track).cue_sheet
    assert sheet is not None
    write_cue_sheet(tmp_path, EPISODE, sheet)
    data = LocalDirectoryBgmSource(tmp_path).load(EPISODE)
    assert data.cue_sheet == sheet
    assert data.catalog == (track,)
    assert data.file_sha256_by_track == {track.track_id: track.file_sha256}
    assert data.track_paths[track.track_id].read_bytes() == AUDIO
