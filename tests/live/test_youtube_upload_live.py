"""実 YouTube への smoke（Phase 6 / ADR-0022）。``AVP_LIVE_YOUTUBE=1`` のときだけ収集される。

- marker 照合（読み取り、quota 数 unit）は ``AVP_LIVE_YOUTUBE=1`` だけで走る
- 実アップロード（**private のみ**、videos.insert quota を消費）はさらに ``CONFIRM_UPLOAD=1``・
  ``YOUTUBE_CHANNEL_ID``（認証中チャンネルと一致すること）
  ``AVP_LIVE_YOUTUBE_VIDEO``（小さな mp4）が必要
- マーカーは**固定**。既に同じマーカーの動画があれば投稿せずその動画を再利用する
  （再実行で2本目を作らない）
- 投稿後は ``processing_status`` を上限つきで照会し、processed・private・チャンネル一致を確かめる
"""

from __future__ import annotations

import asyncio
import os
import pathlib

import httpx
import pytest

from contracts.upload import UPLOAD_PRIVACY_STATUS
from domain.upload.ports import UploadCompleted, UploadExpired, UploadIncomplete
from domain.upload.processing import ProcessingOutcome, classify_processing
from infrastructure.config import Settings
from infrastructure.youtube.oauth import RefreshTokenCredentials
from infrastructure.youtube.uploader import YouTubeResumableUploader

#: 固定マーカー。変えると再実行で別の動画を投稿してしまう
LIVE_MARKER = "avp-live-private-upload-test-v1"
LIVE_TITLE = "[AVP LIVE TEST] private upload"
#: 処理完了を待つ上限（回数 × 間隔）
PROCESSING_POLLS = 40
PROCESSING_POLL_SECONDS = 15


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
        assert await uploader.find_video_by_marker("avp-live-never-uploaded-marker-v0") is None


def _live_metadata() -> dict:
    """契約の metadata builder と同じ形。

    private。notifySubscribers は adapter が false で送る。
    """
    from contracts.upload import build_youtube_metadata
    from infrastructure.youtube.metadata import insert_body

    meta = build_youtube_metadata(
        upload_key="0" * 64,
        title=LIVE_TITLE,
        hook="AVP live test",
        narration="private upload smoke test; safe to delete",
        language="ja",
    )
    body = insert_body(meta)
    snippet = body["snippet"]
    snippet["title"] = LIVE_TITLE
    snippet["tags"] = [LIVE_MARKER]
    snippet["description"] = f"AVP live test (private)\n\n{LIVE_MARKER}"
    assert body["status"]["privacyStatus"] == UPLOAD_PRIVACY_STATUS
    return body


async def test_private_upload_of_a_tiny_file_is_processed_once() -> None:
    if os.environ.get("CONFIRM_UPLOAD") != "1":
        pytest.skip("set CONFIRM_UPLOAD=1 to perform a real private upload")
    channel_id = os.environ.get("YOUTUBE_CHANNEL_ID") or Settings().youtube_channel_id
    if not channel_id:
        pytest.skip("set YOUTUBE_CHANNEL_ID to the channel the credentials belong to")
    path = os.environ.get("AVP_LIVE_YOUTUBE_VIDEO")
    if not path:
        pytest.skip("set AVP_LIVE_YOUTUBE_VIDEO to a small mp4")
    async with httpx.AsyncClient(timeout=120) as client:
        uploader = _uploader(client)
        assert await uploader.own_channel_id() == channel_id, "credentials are for another channel"

        video_id = await uploader.find_video_by_marker(LIVE_MARKER)
        if video_id is None:
            data = pathlib.Path(path).read_bytes()
            session = await uploader.start_session(_live_metadata(), len(data), "video/mp4")
            progress = await uploader.query_status(session)
            while not isinstance(progress, UploadCompleted):
                assert not isinstance(progress, UploadExpired)
                assert isinstance(progress, UploadIncomplete)
                offset = progress.next_offset
                chunk = data[offset : offset + uploader.chunk_bytes]
                progress = await uploader.send_chunk(session, offset, chunk, len(data))
            video_id = progress.video_id
        assert video_id

        verdict = None
        state = None
        for _ in range(PROCESSING_POLLS):
            state = await uploader.processing_status(video_id)
            verdict = classify_processing(state, channel_id)
            if verdict.outcome is not ProcessingOutcome.PENDING:
                break
            await asyncio.sleep(PROCESSING_POLL_SECONDS)
        assert state is not None and verdict is not None
        assert state.found
        assert state.privacy_status == UPLOAD_PRIVACY_STATUS
        assert state.channel_id == channel_id
        assert verdict.outcome is ProcessingOutcome.PROCESSED, verdict.reason
