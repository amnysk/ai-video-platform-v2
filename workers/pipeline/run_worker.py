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
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.temporal.connect import connect_with_retry
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.gate_ports import build_gate_ports
from workers.pipeline.watchdog import DailyWatchdogWorkflow
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

logger = logging.getLogger(__name__)


def build_worker(
    client: Client, activities: PipelineActivities, task_queue: str = PIPELINE_TASK_QUEUE
) -> Worker:
    return Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=task_queue,
        workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow, DailyWatchdogWorkflow],
        activities=activities.activities(),
    )


async def main() -> None:
    settings = Settings()
    client = await connect_with_retry(settings)
    session_factory = session_factory_from_settings(settings)
    activities = PipelineActivities(
        session_factory=session_factory,
        paused_env=settings.paused,
        uploads_paused_env=settings.uploads_paused,
        temporal_client=client,
        # 入口ゲート・保留の結線点（ADR-0058。ADR-0057 の実装はここで差し込む）
        gate_ports=build_gate_ports(session_factory, budget=settings.fal_budget),
    )
    worker = build_worker(client, activities)
    logger.info(
        "pipeline worker listening on %s; paused=%s uploads_paused=%s fal_budget=%s",
        PIPELINE_TASK_QUEUE,
        settings.paused,
        settings.uploads_paused,
        settings.fal_budget.decision_reason,
    )
    async with worker:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
