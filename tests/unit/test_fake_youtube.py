"""FakeVideoUploader 自体の振る舞い（Phase 6）。"""

from __future__ import annotations

import asyncio

import pytest

from domain.upload.ports import UploadCompleted, UploadExpired, UploadIncomplete, VideoUploader
from infrastructure.youtube.errors import YouTubeAuthError, YouTubeQuotaError, YouTubeTransientError
from tests.support.fake_youtube import FakeVideoUploader

META = {"snippet": {"title": "t", "tags": ["marker-1"]}, "status": {"privacyStatus": "private"}}
DATA = bytes(range(10))


async def _upload(fake: FakeVideoUploader, chunk: int = 4) -> str:
    session = await fake.start_session(META, len(DATA), "video/mp4")
    offset = 0
    while True:
        try:
            progress = await fake.send_chunk(
                session, offset, DATA[offset : offset + chunk], len(DATA)
            )
        except YouTubeTransientError:
            progress = await fake.query_status(session)
        if isinstance(progress, UploadCompleted):
            return progress.video_id
        assert isinstance(progress, UploadIncomplete)
        offset = progress.next_offset


def test_is_a_video_uploader() -> None:
    port: VideoUploader = FakeVideoUploader(chunk_bytes=4)
    assert port is not None


async def test_happy_path_creates_one_video_with_bytes_and_tags() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    video_id = await _upload(fake)
    assert fake.videos_created == 1
    assert fake.videos[video_id].data == DATA
    assert await fake.find_video_by_marker("marker-1") == video_id
    assert await fake.find_video_by_marker("other") is None


async def test_transient_chunk_failures_do_not_accept_bytes() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    fake.fail_chunk_sends = 2
    await _upload(fake)
    assert fake.videos_created == 1 and fake.chunk_sends == 5


async def test_lost_completion_response_is_visible_via_status() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    fake.lose_completion_responses = 1
    video_id = await _upload(fake)
    assert fake.videos_created == 1 and fake.status_queries == 1
    assert fake.videos[video_id].data == DATA


async def test_session_expiry_after_bytes() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    fake.expire_session_at_offset = 4
    session = await fake.start_session(META, len(DATA), "video/mp4")
    with pytest.raises(YouTubeTransientError):
        await fake.send_chunk(session, 0, DATA[:4], len(DATA))
    assert await fake.query_status(session) == UploadExpired()
    assert await fake.send_chunk(session, 4, DATA[4:8], len(DATA)) == UploadExpired()
    assert fake.videos_created == 0


async def test_one_shot_errors() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    fake.fail_start_with = YouTubeAuthError("invalid_grant")
    with pytest.raises(YouTubeAuthError):
        await fake.start_session(META, 10, "video/mp4")
    session = await fake.start_session(META, 10, "video/mp4")
    fake.fail_send_with = YouTubeQuotaError("quotaExceeded")
    with pytest.raises(YouTubeQuotaError):
        await fake.send_chunk(session, 0, DATA, 10)
    fake.fail_lookup_with = YouTubeQuotaError("quotaExceeded")
    with pytest.raises(YouTubeQuotaError):
        await fake.find_video_by_marker("marker-1")
    assert isinstance(await fake.send_chunk(session, 0, DATA, 10), UploadCompleted)


async def test_resend_after_completion_does_not_duplicate() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    session = await fake.start_session(META, 10, "video/mp4")
    first = await fake.send_chunk(session, 0, DATA, 10)
    again = await fake.send_chunk(session, 0, DATA, 10)
    assert first == again and fake.videos_created == 1


async def test_concurrent_sends_are_serialized() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    session = await fake.start_session(META, 10, "video/mp4")
    results = await asyncio.gather(*(fake.send_chunk(session, 0, DATA, 10) for _ in range(5)))
    assert all(isinstance(r, UploadCompleted) for r in results)
    assert fake.videos_created == 1


def test_existing_video_is_found() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    video_id = fake.add_existing_video(["m"])
    assert asyncio.run(fake.find_video_by_marker("m")) == video_id


async def test_non_final_chunks_must_be_exactly_chunk_bytes() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    session = await fake.start_session(META, len(DATA), "video/mp4")
    with pytest.raises(ValueError):
        await fake.send_chunk(session, 0, DATA[:3], len(DATA))
    assert await fake.send_chunk(session, 0, DATA[:4], len(DATA)) == UploadIncomplete(4)
    assert fake.content_types == ["video/mp4"]


def test_default_chunk_bytes_follow_the_contract() -> None:
    from contracts.upload import DEFAULT_UPLOAD_CHUNK_BYTES

    assert FakeVideoUploader().chunk_bytes == DEFAULT_UPLOAD_CHUNK_BYTES


async def test_processing_defaults_to_processed_private_own_channel() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    video_id = await _upload(fake)
    state = await fake.processing_status(video_id)
    assert state.found and state.upload_status == "processed"
    assert state.privacy_status == "private" and state.channel_id == fake.channel_id
    assert fake.processing_checks == 1
    assert not (await fake.processing_status("missing")).found


async def test_scripted_processing_states_are_consumed_and_the_last_one_sticks() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    video_id = await _upload(fake)
    fake.script_processing(
        fake.processing_state(upload_status="uploaded", processing_status="processing"),
        fake.processing_state(upload_status="rejected", rejection_reason="duplicate"),
    )
    assert (await fake.processing_status(video_id)).upload_status == "uploaded"
    assert (await fake.processing_status(video_id)).upload_status == "rejected"
    assert (await fake.processing_status(video_id)).upload_status == "rejected"
    assert fake.processing_checks == 3


async def test_processing_one_shot_error() -> None:
    fake = FakeVideoUploader(chunk_bytes=4)
    fake.fail_processing_with = YouTubeQuotaError("quotaExceeded")
    with pytest.raises(YouTubeQuotaError):
        await fake.processing_status("x")
    assert not (await fake.processing_status("x")).found
