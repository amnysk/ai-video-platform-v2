"""本物の有料動画 provider で **4秒の1本だけ** 生成して検証する（課金あり。所有者の明示操作のみ）。

AVP_LIVE_FAL=1 FAL_KEY=... pytest tests/live/test_fal_video_live.py -m live
見積もり: 4 秒 × $0.2419 ≒ $0.97
"""

from __future__ import annotations

import asyncio
import os

import pytest

from domain.errors import MediaValidationError
from domain.production.media import validate_video
from domain.production.ports import JobFailed, JobPending, VideoRequest
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.providers.fal_queue import FalQueueClient
from infrastructure.providers.fal_seedance_video import FalSeedanceVideoGenerator
from infrastructure.providers.fal_storage import FalStorageClient
from tests.support.production import BytesDestination, make_png

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AVP_LIVE_FAL") != "1" or not os.environ.get("FAL_KEY"),
        reason="set AVP_LIVE_FAL=1 and FAL_KEY to run the paid video smoke",
    ),
]


async def test_generates_exactly_one_four_second_vertical_clip() -> None:
    key = os.environ["FAL_KEY"]
    client = FalQueueClient(key)
    storage = FalStorageClient(key)
    generator = FalSeedanceVideoGenerator(client, storage)
    try:
        duration = generator.supported_duration_ms(4000)
        assert duration == 4000
        request = await generator.prepare(
            VideoRequest(
                prompt="A calm blue sky slowly brightening, subtle motion. no text.",
                source_image=make_png(720, 1280, (70, 120, 200)),
                source_image_mime="image/png",
                duration_ms=duration,
                aspect="9:16",
            )
        )
        ref = await generator.submit(request)  # 課金はこの1回だけ
        status = await generator.poll(ref)
        for _ in range(180):
            if not isinstance(status, JobPending):
                break
            await asyncio.sleep(10)
            status = await generator.poll(ref)
        assert not isinstance(status, JobPending | JobFailed), status
        dest = BytesDestination()
        await generator.download(ref, dest)
        info = PillowAvMediaProbe().probe_video(dest.data)
        if info.has_audio:
            raise MediaValidationError("audio stream present despite generate_audio=false")
        validate_video(info, len(dest.data), requested_duration_ms=duration)
    finally:
        await client.aclose()
        await storage.aclose()
