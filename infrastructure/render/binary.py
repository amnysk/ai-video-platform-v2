"""固定版の外部バイナリ（ffmpeg）を使う前に検証する（Phase 5）。

設定された sha256 と一致しないバイナリは**起動しない**。版は ``-version`` の1行目から読む。
結果の identity（engine / version / binary_sha256）は render の input_hash に入る。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_VERSION_LINE = re.compile(r"^(?P<engine>\S+) version (?P<version>\S+)")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
VERSION_TIMEOUT_SECONDS = 30.0


class RenderBinaryError(Exception):
    """バイナリが無い・実行できない・sha256 不一致・版を読めない。

    運用者が正しいバイナリを置けば直る設定の問題。domain の分類へは呼び出し側が変換する。
    """


@dataclass(frozen=True, slots=True)
class BinaryIdentity:
    engine: str
    version: str
    binary_sha256: str
    path: Path


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_binary(
    path: str | Path, expected_sha256: str, *, engine: str = "ffmpeg"
) -> BinaryIdentity:
    """存在・実行権・sha256 を確かめてから ``-version`` を読み、identity を返す。"""
    expected = expected_sha256.strip().lower()
    if not _HEX64.match(expected):
        raise RenderBinaryError("expected sha256 must be 64 lowercase hex characters")
    binary = Path(path)
    if not binary.is_absolute():
        raise RenderBinaryError(f"{engine} path must be absolute: {binary}")
    if not binary.is_file():
        raise RenderBinaryError(f"{engine} binary not found: {binary}")
    if not os.access(binary, os.X_OK):
        raise RenderBinaryError(f"{engine} binary is not executable: {binary}")
    actual = file_sha256(binary)
    if actual != expected:
        raise RenderBinaryError(
            f"{engine} binary sha256 mismatch: expected {expected}, got {actual}"
        )
    try:
        completed = subprocess.run(  # noqa: S603 - 検証済みの絶対パス、argv 固定
            [str(binary), "-hide_banner", "-version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderBinaryError(f"{engine} -version failed: {exc}") from exc
    first = completed.stdout.decode("utf-8", errors="replace").splitlines()[:1]
    match = _VERSION_LINE.match(first[0]) if first else None
    if completed.returncode != 0 or match is None or match["engine"] != engine:
        raise RenderBinaryError(f"cannot read {engine} version (exit {completed.returncode})")
    return BinaryIdentity(
        engine=engine, version=match["version"], binary_sha256=actual, path=binary
    )


__all__ = ["BinaryIdentity", "RenderBinaryError", "file_sha256", "verify_binary"]
