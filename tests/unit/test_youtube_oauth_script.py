"""``scripts/youtube-oauth.py`` の scope 検査（ネットワークに出ない）。"""

from __future__ import annotations

import pathlib

from tests.support.script_loader import load_script_module

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/youtube-oauth.py"

# scripts/ に .pyc を残さない。ロード中のガードだけでなく、ロード後に
# sys.modules へ残さないことも重要（tests/support/script_loader.py 参照）。
oauth = load_script_module("youtube_oauth_script", SCRIPT)
UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
READONLY = "https://www.googleapis.com/auth/youtube.readonly"
ANALYTICS = "https://www.googleapis.com/auth/yt-analytics.readonly"


def test_requests_upload_and_analytics_scopes() -> None:
    assert set(oauth.SCOPES) == {UPLOAD, READONLY, ANALYTICS}


def test_missing_scopes() -> None:
    assert oauth.missing_scopes(f"{UPLOAD} {READONLY} {ANALYTICS}") == []
    assert oauth.missing_scopes(f"{READONLY} {UPLOAD}") == [ANALYTICS]
    assert oauth.missing_scopes(None) == list(oauth.SCOPES)
