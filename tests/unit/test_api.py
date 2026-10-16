"""FastAPI の最小2エンドポイント（INV-1 / INV-16）。"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)


class FakeWorkflowStarter:
    """Temporal client の差し替え。テストから実サーバへ到達させない（INV-18）。"""

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []

    async def start_episode_workflow(self, *, episode_id: str, pipeline: str = "skeleton") -> str:
        workflow_id = f"episode-{episode_id}"
        self.started.append(
            {"episode_id": episode_id, "workflow_id": workflow_id, "pipeline": pipeline}
        )
        return workflow_id


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeWorkflowStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, starter


async def test_post_episodes_creates_planned_episode_and_starts_a_workflow(api) -> None:
    client, starter = api
    response = await client.post("/episodes", json={"topic": "dummy"})

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == EpisodeStatus.PLANNED.value
    assert body["workflow_id"] == f"episode-{body['id']}"
    assert [s["episode_id"] for s in starter.started] == [body["id"]]


async def test_get_episode_returns_state_jobs_and_artifacts(api, session_factory) -> None:
    client, _ = api
    created = (await client.post("/episodes", json={"topic": "dummy"})).json()
    episode_id = created["id"]

    async with session_factory() as session:
        jobs = JobRepository(session)
        artifacts = ArtifactMetadataRepository(session)
        job = await jobs.create(episode_id=episode_id, type=JobType.DUMMY, max_attempts=3)
        digest = "c" * 64
        await artifacts.record(
            episode_id=episode_id,
            artifact_type=ArtifactType.DUMMY,
            schema_version="1.0",
            bucket="artifacts",
            object_key=f"artifacts/{episode_id}/dummy/{digest}.json",
            sha256=digest,
        )
        await session.commit()

    response = await client.get(f"/episodes/{episode_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == episode_id
    assert body["status"] == EpisodeStatus.PLANNED.value
    assert [j["id"] for j in body["jobs"]] == [str(job.id)]
    assert body["jobs"][0]["status"] == JobStatus.QUEUED.value
    assert body["jobs"][0]["max_attempts"] == 3
    assert [a["sha256"] for a in body["artifacts"]] == [digest]


async def test_get_unknown_episode_returns_404(api) -> None:
    client, _ = api
    response = await client.get("/episodes/0192f0c0-0000-7000-8000-0000000000ff")
    assert response.status_code == 404


async def test_get_malformed_episode_id_returns_422(api) -> None:
    client, _ = api
    assert (await client.get("/episodes/not-a-uuid")).status_code == 422


async def test_api_does_not_expose_temporal_internal_state(api, session_factory) -> None:
    """INV-8: Temporal実行状態をdomain stateとして返さない。"""
    client, _ = api
    created = (await client.post("/episodes", json={"topic": "dummy"})).json()
    body = (await client.get(f"/episodes/{created['id']}")).json()

    assert body["status"] in {s.value for s in EpisodeStatus}
    assert "workflow_status" not in body
    assert "run_id" not in body


async def test_episode_status_comes_from_the_database_not_the_workflow(
    api, session_factory
) -> None:
    """INV-7: PostgreSQLがsource of truth。"""
    from domain.episode.transitions import EpisodeEvent

    client, _ = api
    created = (await client.post("/episodes", json={"topic": "dummy"})).json()

    async with session_factory() as session:
        await EpisodeRepository(session).apply_event(created["id"], EpisodeEvent.WORKFLOW_STARTED)
        await session.commit()

    body = (await client.get(f"/episodes/{created['id']}")).json()
    assert body["status"] == EpisodeStatus.IN_PROGRESS.value


# --- Phase 2: どのパイプラインを起動するかを選べる（既定は Phase 1 の骨組み） ---


async def test_post_episodes_defaults_to_the_skeleton_pipeline(api) -> None:
    """既定を変えない。Phase 1 の smoke が壊れないことが最優先。"""
    client, starter = api
    await client.post("/episodes", json={"topic": "dummy"})
    assert starter.started[-1]["pipeline"] == "skeleton"


async def test_post_episodes_can_start_the_script_pipeline(api) -> None:
    client, starter = api
    response = await client.post("/episodes", json={"topic": "縄文土器", "pipeline": "script"})

    assert response.status_code == 202, response.text
    assert starter.started[-1]["pipeline"] == "script"
    assert response.json()["status"] == EpisodeStatus.PLANNED.value


async def test_unknown_pipeline_is_rejected(api) -> None:
    client, _ = api
    response = await client.post("/episodes", json={"topic": "x", "pipeline": "storyboard"})
    assert response.status_code == 422


async def test_api_still_does_not_wait_for_the_script_workflow(api) -> None:
    """INV-16: 生成の完了を待たない。202 を即返す。"""
    client, starter = api
    response = await client.post("/episodes", json={"topic": "x", "pipeline": "script"})
    assert response.status_code == 202
    assert len(starter.started) == 1
