"""render 設定の既定値（Phase 5）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from infrastructure.config import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_render_defaults() -> None:
    s = _settings()
    assert s.render_ffmpeg_path is None and s.render_ffmpeg_sha256 is None
    assert s.render_concurrency == 1
    assert s.render_timeout_seconds == 1800
    assert s.render_min_free_bytes == 10 * 1024**3
    assert s.render_ffmpeg_threads == 4


def test_default_font_sha_matches_the_default_font() -> None:
    s = _settings()
    font = Path(s.render_font_path)
    if not font.is_file():
        pytest.skip("default font not installed")
    assert hashlib.sha256(font.read_bytes()).hexdigest() == s.render_font_sha256
