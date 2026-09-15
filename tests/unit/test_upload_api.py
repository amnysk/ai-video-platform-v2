"""POST /episodes/{id}/upload（ADR-0020 / INV-16）。workflow は起動するだけで待たない。"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.states import EpisodeStatus
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import EpisodeRepository
from tests.support.render_activity import TO_ASSETS_READY

TO_RENDER_READY = [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RENDER_READY]


class FakeUploadStarter:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.already_running = False

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                f"episode-{episode_id}-upload", "UploadWorkflow", run_id="run-1"
            )
        self.started.append(episode_id)
        return f"episode-{episode_id}-upload"


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeUploadStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def _episode(session_factory, events, *, owner: str | None = "upload") -> str:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        for event in events:
            await episodes.apply_event(episode.id, event)
        if owner is not None:
            await episodes.set_workflow_id(episode.id, f"episode-{episode.id}-{owner}:run-1")
        await session.commit()
        return episode.id


async def test_post_upload_from_render_ready_is_202(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_RENDER_READY, owner="render")

    response = await client.post(f"/episodes/{episode_id}/upload")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["workflow_id"] == f"episode-{episode_id}-upload"
    assert body["status"] == EpisodeStatus.RENDER_READY.value
    assert starter.started == [episode_id]


@pytest.mark.parametrize(
    "failure", [EpisodeEvent.RETRYABLE_FAILURE, EpisodeEvent.NEEDS_INPUT_FAILURE]
)
async def test_resume_of_an_upload_stopped_episode_is_202(api, session_factory, failure) -> None:
    client, _ = api
    episode_id = await _episode(
        session_factory, [*TO_RENDER_READY, EpisodeEvent.STAGE_ADMITTED, failure]
    )
    assert (await client.post(f"/episodes/{episode_id}/upload")).status_code == 202


async def test_already_uploaded_is_409(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(
        session_factory,
        [*TO_RENDER_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.UPLOAD_SUCCEEDED],
    )
    response = await client.post(f"/episodes/{episode_id}/upload")
    assert response.status_code == 409 and "already uploaded" in response.json()["detail"]
    assert starter.started == []


@pytest.mark.parametrize("events", [[], TO_ASSETS_READY, TO_ASSETS_READY[:4]])
async def test_inadmissible_state_is_409(api, session_factory, events) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, events)
    response = await client.post(f"/episodes/{episode_id}/upload")
    assert response.status_code == 409 and "upload can start only from" in response.text
    assert starter.started == []


@pytest.mark.parametrize(
    "failure", [EpisodeEvent.RETRYABLE_FAILURE, EpisodeEvent.NEEDS_INPUT_FAILURE]
)
async def test_stopped_by_another_stage_is_409(api, session_factory, failure) -> None:
    client, starter = api
    episode_id = await _episode(
        session_factory, [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, failure], owner="render"
    )
    response = await client.post(f"/episodes/{episode_id}/upload")
    assert response.status_code == 409 and "another stage" in response.json()["detail"]
    assert starter.started == []


async def test_unknown_episode_is_404(api) -> None:
    client, _ = api
    assert (await client.post(f"/episodes/{uuid.uuid4()}/upload")).status_code == 404


async def test_already_running_is_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    episode_id = await _episode(session_factory, TO_RENDER_READY, owner="render")
    response = await client.post(f"/episodes/{episode_id}/upload")
    assert response.status_code == 409 and "already running" in response.json()["detail"]


def test_starter_passes_only_the_episode_id() -> None:
    from apps.api.workflow_starter import TemporalWorkflowStarter, upload_workflow_id
    from infrastructure.config import Settings

    class _Client:
        def __init__(self) -> None:
            self.calls: list = []

        async def start_workflow(self, name, arg, *, id, task_queue):  # noqa: A002
            self.calls.append((name, arg, id, task_queue))

    client = _Client()
    starter = TemporalWorkflowStarter(client, "q", Settings())  # type: ignore[arg-type]
    asyncio.run(starter.start_upload_workflow(episode_id="e"))
    ((name, arg, wid, queue),) = client.calls
    assert (name, wid, queue) == ("UploadWorkflow", upload_workflow_id("e"), "upload")
    # 公開範囲などの投稿設定は API から渡せない（INV-19）
    assert arg == {"episode_id": "e"}
