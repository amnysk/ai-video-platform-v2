"""暫定の BGM 供給元（ローカルディレクトリ）。ADR-0077 §1。"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import pytest

from domain.render.ports import BgmSourceError
from infrastructure.render.bgm_source import LocalDirectoryBgmSource
from tests.support.bgm_catalog import make_track
from tests.support.bgm_render import cue, sheet

EP = str(uuid.uuid4())


def _setup(root: Path, audio: bytes = b"audio-bytes", **track_over):
    digest = hashlib.sha256(audio).hexdigest()
    track = make_track(file_sha256=digest, object_key=f"music/originals/{digest}.mp3", **track_over)
    (root / "music/originals").mkdir(parents=True)
    (root / track.object_key).write_bytes(audio)
    (root / "cues").mkdir()
    (root / "catalog.json").write_text(json.dumps({"tracks": [track.model_dump(mode="json")]}))
    s = sheet(cue(source_sha256=digest))
    (root / "cues" / f"{EP}.json").write_text(s.model_dump_json())
    return track, digest


def test_no_cue_sheet_file_means_no_bgm_without_reading_the_catalog(tmp_path: Path) -> None:
    assert LocalDirectoryBgmSource(tmp_path).load(EP).cue_sheet is None


def test_loads_cue_sheet_catalog_and_the_actual_file_sha(tmp_path: Path) -> None:
    track, digest = _setup(tmp_path)
    data = LocalDirectoryBgmSource(tmp_path).load(EP)
    assert data.cue_sheet is not None and data.catalog == (track,)
    assert data.file_sha256_by_track == {track.track_id: digest}
    assert data.track_paths[track.track_id].read_bytes() == b"audio-bytes"


def test_an_altered_file_reports_a_different_sha_than_the_catalog(tmp_path: Path) -> None:
    track, digest = _setup(tmp_path)
    (tmp_path / track.object_key).write_bytes(b"tampered")
    data = LocalDirectoryBgmSource(tmp_path).load(EP)
    assert data.file_sha256_by_track[track.track_id] != digest


def test_a_missing_audio_file_has_no_sha(tmp_path: Path) -> None:
    track, _ = _setup(tmp_path)
    (tmp_path / track.object_key).unlink()
    data = LocalDirectoryBgmSource(tmp_path).load(EP)
    assert data.file_sha256_by_track[track.track_id] is None and not data.track_paths


def test_broken_json_and_bad_episode_id_are_source_errors(tmp_path: Path) -> None:
    _setup(tmp_path)
    (tmp_path / "catalog.json").write_text("{not json")
    with pytest.raises(BgmSourceError):
        LocalDirectoryBgmSource(tmp_path).load(EP)
    with pytest.raises(BgmSourceError):
        LocalDirectoryBgmSource(tmp_path).load("../../etc")
