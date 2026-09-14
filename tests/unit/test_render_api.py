"""POST /episodes/{id}/render（ADR-0019 / INV-16）。workflow は起動するだけで待たない。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.render import DEFAULT_RENDER_PROFILE_ID
from contracts.states import EpisodeStatus
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import EpisodeRepository
from tests.support.render_activity import TO_ASSETS_READY


class FakeRenderStarter:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []
        self.already_running = False

    async def start_render_workflow(
        self, *, episode_id: str, render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    ) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                f"episode-{episode_id}-render", "RenderWorkflow", run_id="run-1"
            )
        self.started.append((episode_id, render_profile_id))
        return f"episode-{episode_id}-render"


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeRenderStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def _episode(session_factory, events=TO_ASSETS_READY) -> str:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        for event in events:
            await episodes.apply_event(episode.id, event)
        await session.commit()
        return episode.id


async def test_post_render_without_body_uses_the_default_profile(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory)

    response = await client.post(f"/episodes/{episode_id}/render")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["workflow_id"] == f"episode-{episode_id}-render"
    assert body["status"] == EpisodeStatus.ASSETS_READY.value
    assert body["render_profile_id"] == DEFAULT_RENDER_PROFILE_ID
    assert starter.started == [(episode_id, DEFAULT_RENDER_PROFILE_ID)]


async def test_post_render_with_a_profile(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory)

    response = await client.post(
        f"/episodes/{episode_id}/render", json={"render_profile_id": "long_form_horizontal"}
    )

    assert response.status_code == 202, response.text
    assert starter.started == [(episode_id, "long_form_horizontal")]


async def test_unknown_profile_is_422(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory)

    response = await client.post(
        f"/episodes/{episode_id}/render", json={"render_profile_id": "square_tiktok"}
    )

    assert response.status_code == 422, response.text
    assert starter.started == []


@pytest.mark.parametrize(
    "events",
    [
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RENDER_READY],
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RETRYABLE_FAILURE],
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
        [EpisodeEvent.WORKFLOW_STARTED],
    ],
)
async def test_post_render_from_admissible_or_takeover_states(api, session_factory, events):
    client, starter = api
    episode_id = await _episode(session_factory, events)

    response = await client.post(f"/episodes/{episode_id}/render")

    assert response.status_code == 202, response.text


@pytest.mark.parametrize(
    "events",
    [
        [],
        TO_ASSETS_READY[:4],  # storyboard_ready
        [*TO_ASSETS_READY, EpisodeEvent.CANCELLED],
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.PERMANENT_FAILURE],
    ],
)
async def test_post_render_from_inadmissible_state_is_409(api, session_factory, events):
    client, starter = api
    episode_id = await _episode(session_factory, events)

    response = await client.post(f"/episodes/{episode_id}/render")

    assert response.status_code == 409, response.text
    assert "render can start only from" in response.json()["detail"]
    assert starter.started == []


async def test_unknown_episode_is_404(api) -> None:
    client, starter = api
    response = await client.post(f"/episodes/{uuid.uuid4()}/render")
    assert response.status_code == 404


async def test_duplicate_start_is_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    episode_id = await _episode(session_factory)

    response = await client.post(f"/episodes/{episode_id}/render")

    assert response.status_code == 409 and "already running" in response.json()["detail"]


def test_starter_passes_profile_and_engine_timeout() -> None:
    from apps.api.workflow_starter import TemporalWorkflowStarter, render_workflow_id
    from infrastructure.config import Settings

    class _Client:
        def __init__(self) -> None:
            self.calls: list = []

        async def start_workflow(self, name, arg, *, id, task_queue):  # noqa: A002
            self.calls.append((name, arg, id, task_queue))

    client = _Client()
    starter = TemporalWorkflowStarter(client, "q", Settings(render_timeout_seconds=900))  # type: ignore[arg-type]
    asyncio.run(
        starter.start_render_workflow(episode_id="e", render_profile_id="long_form_horizontal")
    )
    ((name, arg, wid, queue),) = client.calls
    assert (name, wid, queue) == ("RenderWorkflow", render_workflow_id("e"), "render")
    assert arg == {
        "episode_id": "e",
        "render_profile_id": "long_form_horizontal",
        "render_timeout_seconds": 900,
    }
