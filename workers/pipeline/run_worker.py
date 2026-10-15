"""Pipeline worker のエントリポイント（ADR-0023）。

queue ``pipeline`` に DailyEpisodeWorkflow / EpisodePipelineWorkflow と状態系 Activity を登録する。
Schedule の登録はしない（``scripts/ensure-daily-schedule.py``）。

``JPMAS2_AGENTIC_WORKFLOWS=true`` のときだけ AgenticEpisodeWorkflow / AgenticLearningWorkflow と
その Activity（``AgenticPipelineActivities``）を足す（ADR-0043 §5、INV-55）。既定の登録は変えない。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.pipeline import PIPELINE_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.agentic_activities import AgenticPipelineActivities
from workers.pipeline.agentic_workflow import AgenticEpisodeWorkflow, AgenticLearningWorkflow
from workers.pipeline.watchdog import DailyWatchdogWorkflow
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

logger = logging.getLogger(__name__)

DEFAULT_WORKFLOWS: tuple[type, ...] = (
    DailyEpisodeWorkflow,
    EpisodePipelineWorkflow,
    DailyWatchdogWorkflow,
)
AGENTIC_WORKFLOWS: tuple[type, ...] = (AgenticEpisodeWorkflow, AgenticLearningWorkflow)


def registrations(
    activities: PipelineActivities, agentic: AgenticPipelineActivities | None = None
) -> tuple[list[type], list[Callable[..., object]]]:
    """worker に登録する (workflow, activity)。``agentic`` が無ければ従来のまま。"""
    workflows = list(DEFAULT_WORKFLOWS)
    acts = list(activities.activities())
    if agentic is not None:
        workflows.extend(AGENTIC_WORKFLOWS)
        acts.extend(agentic.activities())
    return workflows, acts


def build_worker(
    client: Client,
    activities: PipelineActivities,
    task_queue: str = PIPELINE_TASK_QUEUE,
    agentic: AgenticPipelineActivities | None = None,
) -> Worker:
    workflows, acts = registrations(activities, agentic)
    return Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=task_queue,
        workflows=workflows,
        activities=acts,
    )


async def main() -> None:
    settings = Settings()
    client = await connect_with_retry(settings)
    activities = PipelineActivities(
        session_factory=session_factory_from_settings(settings),
        paused_env=settings.paused,
        uploads_paused_env=settings.uploads_paused,
        temporal_client=client,
    )
    agentic: AgenticPipelineActivities | None = None
    if settings.jpmas2_agentic_workflows:
        store = MinioArtifactStore.from_settings(settings)
        await store.ensure_bucket()
        agentic = AgenticPipelineActivities(
            session_factory=session_factory_from_settings(settings),
            store=store,
            bucket=settings.minio_bucket,
        )
    worker = build_worker(client, activities, agentic=agentic)
    logger.info(
        "pipeline worker listening on %s; paused=%s uploads_paused=%s agentic=%s",
        PIPELINE_TASK_QUEUE,
        settings.paused,
        settings.uploads_paused,
        agentic is not None,
    )
    async with worker:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
