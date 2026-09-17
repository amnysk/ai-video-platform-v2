"""Production Video Worker のエントリポイント（ADR-0017 Phase 4C）。

有料 provider（fal）を呼ぶ。``FAL_KEY`` が無ければ起動しない。Workflow は登録しない（INV-3）。
"""

from __future__ import annotations

import asyncio
import logging
import sys

from temporalio.worker import Worker

from contracts.states import PRODUCTION_VIDEO_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.providers.fal_queue import FalQueueClient
from infrastructure.providers.fal_seedance_video import FalSeedanceVideoGenerator
from infrastructure.providers.fal_storage import FalStorageClient
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from infrastructure.workdir import WorkDirectory
from workers.production_video.activities import VideoProductionActivities

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    if not settings.fal_key:
        sys.exit("FAL_KEY is not set: production video worker calls a paid provider")

    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    session_factory = session_factory_from_settings(settings)

    fal_key = settings.fal_key.get_secret_value()
    fal = FalQueueClient(
        fal_key,
        timeout_seconds=settings.production_submit_timeout_seconds,
        read_timeout_seconds=settings.production_fal_read_timeout_seconds,
    )
    storage = FalStorageClient(fal_key, timeout_seconds=settings.production_submit_timeout_seconds)
    generator = FalSeedanceVideoGenerator(fal, storage)
    activities = VideoProductionActivities(
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
        "production video worker listening on task queue %s (profile=%s, concurrency=%s)",
        PRODUCTION_VIDEO_TASK_QUEUE,
        generator.generation_profile_id,
        settings.video_concurrency,
    )
    try:
        async with Worker(
            client,
            task_queue=PRODUCTION_VIDEO_TASK_QUEUE,
            activities=activities.all_activities(),
            max_concurrent_activities=settings.video_concurrency,
        ):
            await asyncio.Event().wait()
    finally:
        await fal.aclose()
        await storage.aclose()


if __name__ == "__main__":
    asyncio.run(main())
