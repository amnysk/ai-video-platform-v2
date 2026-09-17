"""Production Workflow worker のエントリポイント（ADR-0017）。

task queue ``production`` に workflow と**状態系 Activity だけ**を登録する。
画像・音声・動画の Activity はメディア別 worker が各 queue で提供する（INV-3）。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.worker import Worker

from contracts.states import PRODUCTION_WORKFLOW
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.production.activities import ProductionActivities
from workers.production.run_inspector import TemporalWorkflowRunInspector
from workers.production.workflows import ProductionWorkflow

logger = logging.getLogger(__name__)

_, PRODUCTION_TASK_QUEUE = PRODUCTION_WORKFLOW


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    activities = ProductionActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        bucket=settings.minio_bucket,
        run_inspector=TemporalWorkflowRunInspector(client),
    )
    logger.info("production workflow worker listening on task queue %s", PRODUCTION_TASK_QUEUE)
    async with Worker(
        client,
        task_queue=PRODUCTION_TASK_QUEUE,
        workflows=[ProductionWorkflow],
        activities=activities.all_activities(),
    ):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
