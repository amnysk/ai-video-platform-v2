"""render 設定の既定値（Phase 5）。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import contracts.render as render
from infrastructure.config import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_render_defaults() -> None:
    s = _settings()
    assert s.render_ffmpeg_path is None and s.render_ffmpeg_sha256 is None
    #: .env や環境変数に左右されないよう、宣言上の既定値を契約の定数と突き合わせる
    defaults = {name: f.default for name, f in Settings.model_fields.items()}
    assert defaults["render_concurrency"] == render.DEFAULT_RENDER_CONCURRENCY
    assert defaults["render_timeout_seconds"] == render.DEFAULT_RENDER_TIMEOUT_SECONDS
    assert defaults["render_min_free_bytes"] == render.DEFAULT_RENDER_MIN_FREE_BYTES
    assert defaults["render_ffmpeg_threads"] == render.DEFAULT_RENDER_FFMPEG_THREADS


def test_default_font_sha_matches_the_default_font() -> None:
    s = _settings()
    font = Path(s.render_font_path)
    if not font.is_file():
        pytest.skip("default font not installed")
    assert hashlib.sha256(font.read_bytes()).hexdigest() == s.render_font_sha256
