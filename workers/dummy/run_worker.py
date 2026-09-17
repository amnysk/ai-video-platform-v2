"""dummy worker のエントリポイント。

Workerは他のWorkerを呼ばない（INV-3）。次のJobも決めない（INV-4）。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.worker import Worker

from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.dummy.activities import DummyActivities
from workers.dummy.workflows import EpisodeSkeletonWorkflow

logger = logging.getLogger(__name__)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()

    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()

    activities = DummyActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        bucket=settings.minio_bucket,
    )

    logger.info("dummy worker listening on task queue %s", settings.temporal_task_queue)
    async with Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[EpisodeSkeletonWorkflow],
        activities=activities.all_activities(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
