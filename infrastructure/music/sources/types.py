"""音源クライアントの戻り値（ネットワークライブラリに依存しない）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    data: bytes
    sha256: str
    sha1: str
    extension: str


__all__ = ["DownloadedFile"]
