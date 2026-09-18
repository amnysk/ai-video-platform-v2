"""``scripts/youtube-oauth.py`` の scope 検査（ネットワークに出ない）。"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/youtube-oauth.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("youtube_oauth_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # scripts/ に .pyc を残さない（architecture test が scripts/ を全走査する）
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


oauth = _load()
UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"


def test_requests_upload_and_analytics_scopes() -> None:
    assert set(oauth.SCOPES) == {UPLOAD, READONLY, ANALYTICS}


def test_missing_scopes() -> None:
    assert oauth.missing_scopes(f"{UPLOAD} {READONLY} {ANALYTICS}") == []
    assert oauth.missing_scopes(f"{READONLY} {UPLOAD}") == [ANALYTICS]
    assert oauth.missing_scopes(None) == list(oauth.SCOPES)
