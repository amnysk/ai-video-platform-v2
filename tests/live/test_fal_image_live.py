"""本物の有料画像 provider で **1枚だけ** 生成して検証する（課金あり。所有者の明示操作のみ）。

AVP_LIVE_FAL=1 FAL_KEY=... pytest tests/live/test_fal_image_live.py -m live
"""

from __future__ import annotations

import os

import pytest

from domain.production.media import validate_image
from domain.production.ports import ImageRequest, JobFailed, JobPending
from infrastructure.media.normalize import normalize_image_9x16
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.providers.fal_queue import FalQueueClient
from infrastructure.providers.fal_seedream_image import FalSeedreamImageGenerator
from tests.support.production import BytesDestination

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AVP_LIVE_FAL") != "1" or not os.environ.get("FAL_KEY"),
        reason="set AVP_LIVE_FAL=1 and FAL_KEY to run the paid image smoke",
    ),
]


async def test_generates_exactly_one_vertical_image() -> None:
    import asyncio

    client = FalQueueClient(os.environ["FAL_KEY"])
    generator = FalSeedreamImageGenerator(client)
    try:
        ref = await generator.submit(
            ImageRequest(
                prompt="A clay cooking pot over a small campfire at dusk, documentary photo. "
                "no text, no watermark.",
                width=1080,
                height=1920,
                aspect="9:16",
            )
        )
        status = await generator.poll(ref)
        for _ in range(120):
            status = await generator.poll(ref)
            if not isinstance(status, JobPending):
                break
            await asyncio.sleep(5)
        assert not isinstance(status, JobPending | JobFailed), status
        dest = BytesDestination()
        await generator.download(ref, dest)
        normalized = normalize_image_9x16(dest.data)
        info = PillowAvMediaProbe().probe_image(normalized.data)
        validate_image(info, len(normalized.data))
    finally:
        await client.aclose()
