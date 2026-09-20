"""Pipeline worker のエントリポイント（ADR-0023）。

queue ``pipeline`` に DailyEpisodeWorkflow / EpisodePipelineWorkflow と状態系 Activity を登録する。
Schedule の登録はしない（``scripts/ensure-daily-schedule.py``）。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.pipeline import PIPELINE_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.temporal.connect import connect_with_retry
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.watchdog import DailyWatchdogWorkflow
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

logger = logging.getLogger(__name__)


def build_worker(
    client: Client, activities: PipelineActivities, task_queue: str = PIPELINE_TASK_QUEUE
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow, DailyWatchdogWorkflow],
        activities=activities.activities(),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    client = await connect_with_retry(settings)
    activities = PipelineActivities(
        session_factory=session_factory_from_settings(settings),
        paused_env=settings.paused,
        uploads_paused_env=settings.uploads_paused,
        temporal_client=client,
    )
    worker = build_worker(client, activities)
    logger.info(
        "pipeline worker listening on %s; paused=%s uploads_paused=%s",
        PIPELINE_TASK_QUEUE,
        settings.paused,
        settings.uploads_paused,
    )
    async with worker:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
