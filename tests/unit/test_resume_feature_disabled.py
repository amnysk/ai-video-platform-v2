"""固定計画を持つ Episode の再開は、agentic の flag が OFF なら何も起動しない（ADR-0044 §3-3）。

API は 409 ``feature_disabled``、介入 worker は ``not_needed`` / ``feature_disabled`` を記録する。
どちらも agentic を起動せず、legacy の pipeline にも落とさない。
各テストの理由: docs/testing/jpmas2-p2a-fixes-rationale.md
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from contracts.states import EpisodeStatus
from infrastructure.config import Settings
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.intervention import NOW, make_episode
from tests.unit import test_intervention_agentic_resume as base
from tests.unit.test_resume_api import TO_STORYBOARD_READY, _episode
from tests.unit.test_resume_api_agentic import AgenticStarter, _pin
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import InterventionCommandWorkflow

# ------------------------------------------------------------------ API


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import (
        get_artifact_store_factory,
        get_session_factory,
        get_settings,
        get_workflow_starter,
    )
    from apps.api.main import create_app

    store = InMemoryArtifactStore()
    starter = AgenticStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    app.dependency_overrides[get_artifact_store_factory] = lambda: lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(jpmas2_agentic_workflows=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter, store


async def test_api_resume_of_a_pinned_episode_with_the_flag_off_is_409_and_starts_nothing(
    api, session_factory
) -> None:
    client, starter, store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    await _pin(session_factory, store, episode_id)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["error"] == "feature_disabled"
    assert response.json()["detail"]["message"]
    assert starter.agentic_started == [] and starter.pipeline_started == []  # legacy にも落とさない


async def test_api_resume_of_a_legacy_episode_is_unaffected_by_the_flag(
    api, session_factory
) -> None:
    client, starter, _store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert starter.pipeline_started == [(episode_id, "production")]


# ------------------------------------------------------------------ worker


@pytest_asyncio.fixture
async def worker_env(session_factory, artifact_store):
    base.WORLD = base.World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue, held_queue = f"intervention-{uuid.uuid4()}", f"held-{uuid.uuid4()}"
        activities = InterventionActivities(
            session_factory=session_factory,
            store=artifact_store,
            budget_policy=BudgetPolicy(),
            pipeline_options=base.PipelineOptions(pipeline_task_queue=held_queue),
            command_task_queue=queue,
            clock=lambda: NOW,
            agentic_enabled=False,
        )
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[InterventionCommandWorkflow],
            activities=activities.activities(),
        )
        held = Worker(
            wf_env.client,
            task_queue=held_queue,
            workflows=[base.HeldLegacy, base.HeldAgentic],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, held:
            with wf_env.auto_time_skipping_disabled():
                yield base.Env(wf_env.client, queue, held_queue)


async def test_worker_resume_of_a_pinned_episode_with_the_flag_off_starts_nothing(
    worker_env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await base._pin(session_factory, artifact_store, episode_id)

    result, _history = await base._resume(worker_env, session_factory, artifact_store, episode_id)

    assert base.WORLD.started == []  # agentic も legacy も起動しない
    pipeline: dict[str, Any] = result["pipeline"]
    assert pipeline["outcome"] == "not_needed" and pipeline["reason"] == "feature_disabled"


async def test_worker_resume_of_a_legacy_episode_with_the_flag_off_still_starts_the_pipeline(
    worker_env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)

    result, _history = await base._resume(worker_env, session_factory, artifact_store, episode_id)

    assert result["pipeline"]["outcome"] == "started"
    assert [n for n, _i, _p in base.WORLD.started] == ["EpisodePipelineWorkflow"]
    await base._release(worker_env, result["pipeline"]["workflow_id"])
