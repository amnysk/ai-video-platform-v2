"""POST /episodes/{id}/resume も、pin 済みの計画があれば ``AgenticEpisodeWorkflow`` を起動する。

介入 command の resume（``test_intervention_agentic_resume.py``）と同じ決定を API でも行う。
各テストの理由: docs/testing/jpmas2-fixa-rationale.md
"""

from __future__ import annotations

import asyncio

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.agentic_pipeline import PinWorkflowPlanRequest, agentic_episode_workflow_id
from infrastructure.config import Settings
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.unit.test_resume_api import TO_STORYBOARD_READY, FakeResumeStarter, _episode
from workers.pipeline.agentic_activities import AgenticPipelineActivities


class AgenticStarter(FakeResumeStarter):
    def __init__(self) -> None:
        super().__init__()
        self.agentic_started: list[tuple[str, str, str]] = []

    async def start_agentic_workflow(
        self, *, episode_id: str, template_id: str, template_version: str
    ) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                agentic_episode_workflow_id(episode_id), "AgenticEpisodeWorkflow", run_id="r"
            )
        self.agentic_started.append((episode_id, template_id, template_version))
        return agentic_episode_workflow_id(episode_id)


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
    # flag OFF の再開は 409（test_resume_feature_disabled.py）。ここは ON の環境の挙動を検査する
    app.dependency_overrides[get_settings] = lambda: Settings(jpmas2_agentic_workflows=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter, store


async def _pin(session_factory, store, episode_id: str) -> None:
    await AgenticPipelineActivities(
        session_factory=session_factory, store=store, bucket="artifacts"
    ).pin_workflow_plan(
        PinWorkflowPlanRequest(
            episode_id=episode_id, template_id="shorts_agentic", template_version="1"
        )
    )


async def test_resume_of_an_agentic_episode_starts_the_agentic_workflow(
    api, session_factory
) -> None:
    client, starter, store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    await _pin(session_factory, store, episode_id)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert response.json()["workflow_id"] == agentic_episode_workflow_id(episode_id)
    assert starter.agentic_started == [(episode_id, "shorts_agentic", "1")]
    assert starter.pipeline_started == []


async def test_resume_of_a_legacy_episode_never_touches_the_store(api, session_factory) -> None:
    client, starter, _store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert starter.pipeline_started == [(episode_id, "production")]
    assert starter.agentic_started == []


async def test_resume_of_an_agentic_episode_while_running_is_409(api, session_factory) -> None:
    client, starter, store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    await _pin(session_factory, store, episode_id)
    starter.already_running = True

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409
    assert starter.agentic_started == []


def test_the_api_starter_reuses_a_closed_agentic_id_but_never_a_running_one() -> None:
    """ALLOW_DUPLICATE: 閉じた実行の id は再利用でき、実行中の同じ id は Temporal が拒否する。"""
    from apps.api.workflow_starter import TemporalWorkflowStarter
    from infrastructure.config import Settings

    class _Client:
        def __init__(self) -> None:
            self.calls: list = []

        async def start_workflow(self, name, arg, *, id, task_queue, id_reuse_policy):  # noqa: A002
            self.calls.append((name, arg, id, task_queue, id_reuse_policy))

    client = _Client()
    starter = TemporalWorkflowStarter(client, "q", Settings())  # type: ignore[arg-type]
    asyncio.run(
        starter.start_agentic_workflow(
            episode_id="e", template_id="shorts_agentic", template_version="1"
        )
    )
    ((name, arg, wid, _queue, policy),) = client.calls
    assert (name, wid) == ("AgenticEpisodeWorkflow", agentic_episode_workflow_id("e"))
    assert policy is WorkflowIDReusePolicy.ALLOW_DUPLICATE
    assert arg["episode_id"] == "e" and arg["template_id"] == "shorts_agentic"
