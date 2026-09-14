"""実 YouTube への smoke（Phase 6）。``AVP_LIVE_YOUTUBE=1`` のときだけ収集される。

- marker 照合（読み取り、quota 数 unit）は ``AVP_LIVE_YOUTUBE=1`` だけで走る
- 実アップロード（**private のみ**、videos.insert quota を消費）はさらに ``CONFIRM_UPLOAD=1`` と
  ``AVP_LIVE_YOUTUBE_VIDEO``（小さな mp4 のパス）が必要
"""

from __future__ import annotations

import os
import pathlib
import uuid

import httpx
import pytest

from domain.upload.ports import UploadCompleted, UploadExpired, UploadIncomplete
from infrastructure.config import Settings
from infrastructure.youtube.oauth import RefreshTokenCredentials
from infrastructure.youtube.uploader import YouTubeResumableUploader


def _uploader(client: httpx.AsyncClient) -> YouTubeResumableUploader:
    settings = Settings()
    if not (
        settings.youtube_client_id
        and settings.youtube_client_secret
        and settings.youtube_refresh_token_path
    ):
        pytest.skip("youtube OAuth settings are not configured")
    creds = RefreshTokenCredentials.from_file(
        settings.youtube_client_id,
        settings.youtube_client_secret,
        settings.youtube_refresh_token_path,
        client=client,
    )
    return YouTubeResumableUploader(creds, client=client, chunk_bytes=settings.youtube_chunk_bytes)


async def test_marker_lookup_for_unknown_marker_returns_none() -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        uploader = _uploader(client)
        assert await uploader.find_video_by_marker(f"avp-live-{uuid.uuid4().hex}") is None


async def test_private_upload_of_a_tiny_file() -> None:
    if os.environ.get("CONFIRM_UPLOAD") != "1":
        pytest.skip("set CONFIRM_UPLOAD=1 to perform a real private upload")
    path = os.environ.get("AVP_LIVE_YOUTUBE_VIDEO")
    if not path:
        pytest.skip("set AVP_LIVE_YOUTUBE_VIDEO to a small mp4")
    data = pathlib.Path(path).read_bytes()
    marker = f"avp-live-{uuid.uuid4().hex[:16]}"
    metadata = {
        "snippet": {"title": "avp live smoke (private)", "description": "", "tags": [marker]},
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,
        },
    }
    async with httpx.AsyncClient(timeout=120) as client:
        uploader = _uploader(client)
        session = await uploader.start_session(metadata, len(data), "video/mp4")
        offset = 0
        progress = await uploader.query_status(session)
        while not isinstance(progress, UploadCompleted):
            assert not isinstance(progress, UploadExpired)
            assert isinstance(progress, UploadIncomplete)
            offset = progress.next_offset
            chunk = data[offset : offset + uploader.chunk_bytes]
            progress = await uploader.send_chunk(session, offset, chunk, len(data))
        assert progress.video_id
