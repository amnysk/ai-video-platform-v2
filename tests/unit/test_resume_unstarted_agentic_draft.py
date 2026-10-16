"""未開始の agentic draft（brief のみ・計画未 pin）は resume で始められない（ADR-0044 §3-4）。

draft は ``read_pinned_agentic_template`` では legacy に見える。resume が legacy の
``EpisodePipelineWorkflow`` を起動しないことを、API・介入 worker・判定関数で固定する。
各テストの理由: docs/testing/jpmas2-p2a-integration-rationale.md
"""

from __future__ import annotations

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from contracts.states import ArtifactType, EpisodeStatus
from infrastructure.agents.pinned_plan import has_unstarted_agentic_draft
from infrastructure.config import Settings
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.agent_stage import user_brief
from tests.support.intervention import make_episode, store_artifact
from tests.unit import test_intervention_agentic_resume as base
from tests.unit.test_intervention_agentic_resume import env as worker_env  # noqa: F401  (fixture)
from tests.unit.test_resume_api import TO_STORYBOARD_READY, _episode
from tests.unit.test_resume_api_agentic import AgenticStarter, _pin

# ------------------------------------------------------------------ helper


async def test_helper_is_true_only_with_a_brief_and_no_plan(
    session, session_factory, artifact_store
) -> None:
    bare = await make_episode(session, EpisodeStatus.PLANNED)
    draft = await make_episode(session, EpisodeStatus.PLANNED)
    await store_artifact(session, artifact_store, draft, ArtifactType.USER_BRIEF, user_brief())
    started = await make_episode(session, EpisodeStatus.PLANNED)
    await store_artifact(session, artifact_store, started, ArtifactType.USER_BRIEF, user_brief())
    await _pin(session_factory, artifact_store, started)

    assert await has_unstarted_agentic_draft(session_factory, bare) is False
    assert await has_unstarted_agentic_draft(session_factory, draft) is True
    assert await has_unstarted_agentic_draft(session_factory, started) is False


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
    app.dependency_overrides[get_settings] = lambda: Settings(jpmas2_agentic_workflows=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter, store


async def test_api_resume_of_an_unstarted_agentic_draft_is_409_and_starts_nothing(
    api, session_factory
) -> None:
    client, starter, store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    async with session_factory() as s:
        await store_artifact(s, store, episode_id, ArtifactType.USER_BRIEF, user_brief())

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_state"
    assert detail["reason"] == "agentic_draft_not_started" and detail["message"]
    assert starter.agentic_started == [] and starter.pipeline_started == []


async def test_api_resume_of_a_legacy_episode_still_starts_the_pipeline(
    api, session_factory
) -> None:
    client, starter, _store = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert starter.pipeline_started == [(episode_id, "production")]


# ------------------------------------------------------------------ worker


async def test_worker_resume_of_an_unstarted_agentic_draft_starts_nothing(
    worker_env,  # noqa: F811
    session,
    session_factory,
    artifact_store,
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await store_artifact(session, artifact_store, episode_id, ArtifactType.USER_BRIEF, user_brief())

    result, _history = await base._resume(worker_env, session_factory, artifact_store, episode_id)

    assert base.WORLD.started == []  # agentic も legacy も起動しない
    pipeline = result["pipeline"]
    assert pipeline["outcome"] == "not_needed" and pipeline["reason"] == "invalid_state"
