"""介入 command worker のエントリポイント（ADR-0041 §3）。

queue ``intervention`` に InterventionCommandWorkflow / InterventionRedeliveryWorkflow と
Activity を登録する。Schedule の登録はしない（``infrastructure/temporal/schedules.py`` の
``ensure_intervention_redelivery_schedule``）。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.intervention import INTERVENTION_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.intervention.budget import budget_policy_from_settings
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from infrastructure.temporal.pipeline_options import pipeline_options_from_settings
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import (
    InterventionCommandWorkflow,
    InterventionRedeliveryWorkflow,
)

logger = logging.getLogger(__name__)


def build_worker(
    client: Client, activities: InterventionActivities, task_queue: str = INTERVENTION_TASK_QUEUE
) -> Worker:
    return Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=task_queue,
        workflows=[InterventionCommandWorkflow, InterventionRedeliveryWorkflow],
        activities=activities.activities(),
    )


async def main() -> None:
    settings = Settings()
    if settings.budget_usd_jpy_rate is None:
        logger.warning(
            "BUDGET_USD_JPY_RATE is not set: paid changes are rejected as budget_unverifiable "
            "until it is configured"
        )
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    activities = InterventionActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        budget_policy=budget_policy_from_settings(settings),
        # pipeline の production 枠・render profile・queue は日次 Schedule・API と同じ関数から
        pipeline_options=pipeline_options_from_settings(settings),
    )
    worker = build_worker(client, activities)
    logger.info("intervention worker listening on %s", INTERVENTION_TASK_QUEUE)
    async with worker:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
