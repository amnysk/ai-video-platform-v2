"""POST /episodes/{id}/storyboard（ADR-0015 / INV-16）。workflow は起動するだけで待たない。"""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.states import STORYBOARD_WORKFLOW, EpisodeStatus
from infrastructure.db.repositories import EpisodeRepository


class FakeStoryboardStarter:
    def __init__(self) -> None:
        self.storyboard_started: list[str] = []
        self.already_running = False

    async def start_episode_workflow(self, *, episode_id: str, pipeline: str = "skeleton") -> str:
        raise AssertionError("storyboard endpoint must not start the creation pipeline")

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                f"episode-{episode_id}-storyboard", "StoryboardWorkflow", run_id="run-1"
            )
        self.storyboard_started.append(episode_id)
        return f"episode-{episode_id}-storyboard"


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeStoryboardStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def test_post_storyboard_starts_the_workflow_and_returns_202(api, session_factory) -> None:
    client, starter = api
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="x")
        await session.commit()

    response = await client.post(f"/episodes/{episode.id}/storyboard")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["workflow_id"] == f"episode-{episode.id}-storyboard"
    assert body["episode_id"] == episode.id
    assert body["status"] == EpisodeStatus.PLANNED.value
    assert starter.storyboard_started == [episode.id]


async def test_post_storyboard_for_unknown_episode_is_404(api) -> None:
    client, starter = api
    response = await client.post(f"/episodes/{uuid.uuid4()}/storyboard")
    assert response.status_code == 404
    assert starter.storyboard_started == []


async def test_post_storyboard_while_already_running_is_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="x")
        await session.commit()

    response = await client.post(f"/episodes/{episode.id}/storyboard")

    assert response.status_code == 409, response.text
    assert "already running" in response.json()["detail"]
    assert starter.storyboard_started == []


def test_temporal_starter_uses_the_single_storyboard_definition() -> None:
    from apps.api.workflow_starter import storyboard_workflow_id

    assert STORYBOARD_WORKFLOW == ("StoryboardWorkflow", "storyboard")
    assert storyboard_workflow_id("abc") == "episode-abc-storyboard"
