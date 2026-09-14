"""POST /episodes/{id}/production（ADR-0017 / INV-16）。workflow は起動するだけで待たない。"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.states import EpisodeStatus
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import EpisodeRepository

TO_STORYBOARD_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
]


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


async def _episode(session_factory, events=TO_STORYBOARD_READY) -> str:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        for event in events:
            await episodes.apply_event(episode.id, event)
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
    assert body["status"] == EpisodeStatus.STORYBOARD_READY.value
    assert starter.production_started == [episode_id]


@pytest.mark.parametrize(
    "events",
    [
        [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RETRYABLE_FAILURE],
        [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
        [EpisodeEvent.WORKFLOW_STARTED],  # in_progress: 閉じた run の引き継ぎは admit が判定
    ],
)
async def test_post_production_from_resumable_states_starts_the_workflow(
    api, session_factory, events
) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, events)

    response = await client.post(f"/episodes/{episode_id}/production")

    assert response.status_code == 202, response.text
    assert starter.production_started == [episode_id]


@pytest.mark.parametrize(
    "events",
    [
        [],  # planned
        [EpisodeEvent.WORKFLOW_STARTED, EpisodeEvent.SCRIPT_READY],
        [*TO_STORYBOARD_READY, EpisodeEvent.CANCELLED],
        [*TO_STORYBOARD_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.PERMANENT_FAILURE],
    ],
)
async def test_post_production_from_inadmissible_state_is_409(api, session_factory, events):
    client, starter = api
    episode_id = await _episode(session_factory, events)

    response = await client.post(f"/episodes/{episode_id}/production")

    assert response.status_code == 409, response.text
    assert "production can start only from" in response.json()["detail"]
    assert starter.production_started == []


def test_starter_passes_round_and_reawait_budgets_from_settings() -> None:
    import asyncio

    from apps.api.workflow_starter import TemporalWorkflowStarter
    from infrastructure.config import Settings

    class _Client:
        def __init__(self) -> None:
            self.args: list = []

        async def start_workflow(self, name, arg, *, id, task_queue):  # noqa: A002
            self.args.append(arg)

    client = _Client()
    settings = Settings(
        production_image_max_rounds=4,
        production_video_max_rounds=1,
        production_await_reexecutions=2,
    )
    starter = TemporalWorkflowStarter(client, "q", settings)  # type: ignore[arg-type]
    asyncio.run(starter.start_production_workflow(episode_id="e"))
    (arg,) = client.args
    assert (arg["image_max_rounds"], arg["video_max_rounds"], arg["await_reexecutions"]) == (
        4,
        1,
        2,
    )


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
