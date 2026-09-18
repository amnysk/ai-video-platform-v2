"""子プロセス: 本物の Daily/EpisodePipeline workflow + 本物の PipelineActivities と fake 工程。

queue は ``DURABILITY_QUEUE``（pipeline）と ``DURABILITY_STAGE_QUEUE``（fake 工程）。どちらも一意。
Topic Planner は本物の workflow と Activity（同じ一時スキーマ）。
LLM だけ固定出力の fake、Analytics は無し。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from contracts.topic_planning import DEFAULT_PLANNER_POLICY
from tests.support.durability.common import (
    ENV_DB_URL,
    ENV_QUEUE,
    ENV_SCHEMA,
    ENV_STAGE_QUEUE,
    mark_ready,
    schema_session_factory,
)
from tests.support.durability.stages import FAKE_STAGES, advance_activity
from tests.support.fakes import FakeStoryGenerator
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.run_worker import build_worker
from workers.planning.topic_activities import TopicPlannerActivities
from workers.planning.topic_workflows import TopicPlannerWorkflow

#: 契約の最小件数（``DEFAULT_PLANNER_POLICY.candidate_count_min``）の互いに別の候補
_TOPIC_SUBJECTS = (
    "edo_firefighters",
    "sumo_salt",
    "tea_ceremony",
    "rice_tax",
    "ninja_myths",
    "heian_poetry",
    "castle_towns",
    "shinto_shrines",
    "samurai_armor",
    "meiji_railways",
    "kabuki_theater",
    "ukiyo_e_prints",
    "sengoku_spies",
    "edo_bathhouses",
    "kamakura_zen",
    "ancient_haniwa",
    "muromachi_noh",
    "modern_manga",
    "edo_postal_relays",
    "heian_court_games",
)
TOPIC_BATCH = json.dumps(
    {
        "candidates": [
            {
                "topic": f"The surprising story of {subject.replace('_', ' ')}",
                "subject": subject,
                "entities": [],
                "era": "edo",
                "theme": "daily_life",
                "angle": "reason",
                "hook": f"Nobody tells you this about {subject.replace('_', ' ')}",
                "visual_concept": f"Scenes of {subject.replace('_', ' ')}",
                "reason": "Surprising and visual",
                "audience_fit": 0.8,
                "visual_fit": 0.9,
            }
            for subject in _TOPIC_SUBJECTS[: DEFAULT_PLANNER_POLICY.candidate_count_min]
        ]
    }
)


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
    planner = TopicPlannerActivities(
        session_factory=factory,
        generator=FakeStoryGenerator(output=TOPIC_BATCH),
        analytics=None,
        analytics_provider_id="durability",
        clock=lambda: datetime.now(UTC),
        timeout_seconds=30,
    )
    stages = Worker(
        client,
        task_queue=stage_queue,
        workflows=[*FAKE_STAGES, TopicPlannerWorkflow],
        activities=[advance_activity(factory), *planner.all_activities()],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with pipeline, stages:
        mark_ready()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
