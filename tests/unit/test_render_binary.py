"""verify_binary: sha256 が一致しないバイナリは起動しない。"""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from infrastructure.render.binary import RenderBinaryError, verify_binary


def _fake(tmp_path: Path, body: str, *, executable: bool = True) -> tuple[Path, str]:
    path = tmp_path / "ffmpeg"
    path.write_text(f"#!/bin/sh\n{body}\n")
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_returns_identity_with_parsed_version(tmp_path: Path) -> None:
    path, sha = _fake(tmp_path, "echo 'ffmpeg version 7.1.1 Copyright (c) 2000-2025'")
    identity = verify_binary(path, sha.upper())
    assert (identity.engine, identity.version, identity.binary_sha256) == ("ffmpeg", "7.1.1", sha)


def test_sha_mismatch_is_rejected_before_execution(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    path, _ = _fake(tmp_path, f"touch {marker}; echo 'ffmpeg version 7.1.1'")
    with pytest.raises(RenderBinaryError, match="mismatch"):
        verify_binary(path, "0" * 64)
    assert not marker.exists()


@pytest.mark.parametrize("sha", ["", "abc", "g" * 64])
def test_malformed_expected_sha_is_rejected(tmp_path: Path, sha: str) -> None:
    path, _ = _fake(tmp_path, "true")
    with pytest.raises(RenderBinaryError):
        verify_binary(path, sha)


def test_missing_relative_or_non_executable(tmp_path: Path) -> None:
    with pytest.raises(RenderBinaryError, match="not found"):
        verify_binary(tmp_path / "nope", "0" * 64)
    with pytest.raises(RenderBinaryError, match="absolute"):
        verify_binary("ffmpeg", "0" * 64)
    path, sha = _fake(tmp_path, "true", executable=False)
    with pytest.raises(RenderBinaryError, match="not executable"):
        verify_binary(path, sha)


@pytest.mark.parametrize("body", ["echo 'garbage'", "echo 'ffprobe version 7.1.1'", "exit 1"])
def test_unreadable_version_is_rejected(tmp_path: Path, body: str) -> None:
    path, sha = _fake(tmp_path, body)
    with pytest.raises(RenderBinaryError, match="version"):
        verify_binary(path, sha)
