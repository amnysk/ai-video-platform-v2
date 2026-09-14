"""POST /episodes/{id}/production（ADR-0017 / INV-16）。workflow は起動するだけで待たない。"""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.states import EpisodeStatus
from infrastructure.db.repositories import EpisodeRepository


class FakeProductionStarter:
    def __init__(self) -> None:
        self.production_started: list[str] = []
        self.already_running = False

    async def start_episode_workflow(self, *, episode_id: str, pipeline: str = "skeleton") -> str:
        raise AssertionError("production endpoint must not start the creation pipeline")

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("production endpoint must not start storyboard")

    async def start_production_workflow(self, *, episode_id: str) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                f"episode-{episode_id}-production", "ProductionWorkflow", run_id="run-1"
            )
        self.production_started.append(episode_id)
        return f"episode-{episode_id}-production"


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeProductionStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def _episode(session_factory) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="x")
        await session.commit()
        return episode.id


async def test_post_production_starts_the_workflow_and_returns_202(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory)

    response = await client.post(f"/episodes/{episode_id}/production")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["workflow_id"] == f"episode-{episode_id}-production"
    assert body["episode_id"] == episode_id
    assert body["status"] == EpisodeStatus.PLANNED.value
    assert starter.production_started == [episode_id]


async def test_post_production_for_unknown_episode_is_404(api) -> None:
    client, starter = api
    response = await client.post(f"/episodes/{uuid.uuid4()}/production")
    assert response.status_code == 404
    assert starter.production_started == []


async def test_post_production_while_already_running_is_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    episode_id = await _episode(session_factory)

    response = await client.post(f"/episodes/{episode_id}/production")

    assert response.status_code == 409, response.text
    assert "already running" in response.json()["detail"]
    assert starter.production_started == []


def test_production_workflow_id_is_stable() -> None:
    from apps.api.workflow_starter import production_workflow_id

    assert production_workflow_id("abc") == "episode-abc-production"
