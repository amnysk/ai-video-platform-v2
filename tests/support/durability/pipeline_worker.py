"""子プロセス: 本物の Daily/EpisodePipeline workflow + 本物の PipelineActivities と fake 工程。

queue は ``DURABILITY_QUEUE``（pipeline）と ``DURABILITY_STAGE_QUEUE``（fake 工程）。どちらも一意。
"""

from __future__ import annotations

import asyncio
import logging
import os

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from tests.support.durability.common import (
    ENV_DB_URL,
    ENV_QUEUE,
    ENV_SCHEMA,
    ENV_STAGE_QUEUE,
    mark_ready,
    schema_session_factory,
)
from tests.support.durability.stages import FAKE_STAGES, advance_activity
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.run_worker import build_worker


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    queue = os.environ[ENV_QUEUE]
    stage_queue = os.environ[ENV_STAGE_QUEUE]
    assert queue not in {"pipeline"} and stage_queue not in {"script", "upload", "render"}
    factory = schema_session_factory(os.environ[ENV_DB_URL], os.environ[ENV_SCHEMA])
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"], namespace="default")
    activities = PipelineActivities(
        session_factory=factory, paused_env=False, uploads_paused_env=False
    )
    pipeline = build_worker(client, activities, task_queue=queue)
    stages = Worker(
        client,
        task_queue=stage_queue,
        workflows=FAKE_STAGES,
        activities=[advance_activity(factory)],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with pipeline, stages:
        mark_ready()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
