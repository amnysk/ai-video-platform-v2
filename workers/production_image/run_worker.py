"""Production Image Worker のエントリポイント（ADR-0017 Phase 4A）。

有料 provider（fal）を呼ぶ。``FAL_KEY`` が無ければ起動しない。Workflow は登録しない
（workflow は ``workers/production`` が持つ。共有するのは contracts の名前と型だけ / INV-3）。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import PRODUCTION_IMAGE_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.providers.fal_queue import FalQueueClient
from infrastructure.providers.fal_seedream_image import FalSeedreamImageGenerator
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from workers.production_image.activities import ImageProductionActivities

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx は URL を INFO で出す。キーは header なので出ないが、ノイズを抑える
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if not settings.fal_key:
        sys.exit("FAL_KEY is not set: production image worker calls a paid provider")

    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    session_factory = session_factory_from_settings(settings)

    fal = FalQueueClient(
        settings.fal_key, timeout_seconds=settings.production_submit_timeout_seconds
    )
    generator = FalSeedreamImageGenerator(fal)
    activities = ImageProductionActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        probe=PillowAvMediaProbe(),
        runner=PaidJobRunner(
            session_factory=session_factory,
            store=store,
            workdir=WorkDirectory(settings.ai_video_work_root),
        ),
        bucket=settings.minio_bucket,
        poll_interval_seconds=settings.production_poll_interval_seconds,
        await_deadline_seconds=settings.production_await_timeout_seconds,
    )

    logger.info(
        "production image worker listening on task queue %s (profile=%s, concurrency=%s)",
        PRODUCTION_IMAGE_TASK_QUEUE,
        generator.generation_profile_id,
        settings.image_concurrency,
    )
    try:
        async with Worker(
            client,
            task_queue=PRODUCTION_IMAGE_TASK_QUEUE,
            activities=activities.all_activities(),
            max_concurrent_activities=settings.image_concurrency,
        ):
            await asyncio.Event().wait()
    finally:
        await fal.aclose()


if __name__ == "__main__":
    asyncio.run(main())
