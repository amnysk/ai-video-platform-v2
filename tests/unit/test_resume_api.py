"""GET /episodes/{id}/resume/plan・POST /episodes/{id}/resume（ADR-0032 / INV-16）。

GET は読み取り専用の dry-run: WorkflowStarter を依存に注入しない（構造的に workflow を
起動できない）。POST は同じ計画を再検証してから ``EpisodePipelineWorkflow`` を起動するだけで、
完了を待たない。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.states import ProviderCall
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from tests.support.render_activity import TO_ASSETS_READY

TO_STORYBOARD_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
]
TO_RENDER_READY = [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.RENDER_READY]


class FakeResumeStarter:
    """他の全メソッドは呼ばれたら失敗させる: resume の経路がそれらを使わないことの検査。"""

    def __init__(self) -> None:
        self.pipeline_started: list[tuple[str, str]] = []
        self.already_running = False

    async def start_episode_workflow(self, *, episode_id: str, pipeline: str = "skeleton") -> str:
        raise AssertionError("resume must not start the creation pipeline")

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start storyboard directly")

    async def start_production_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start production directly")

    async def start_render_workflow(self, *, episode_id: str, render_profile_id: str = "x") -> str:
        raise AssertionError("resume must not start render directly")

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start upload directly")

    async def start_pipeline_workflow(self, *, episode_id: str, start_stage: str) -> str:
        if self.already_running:
            raise WorkflowAlreadyStartedError(
                f"episode-{episode_id}-pipeline", "EpisodePipelineWorkflow", run_id="run-1"
            )
        self.pipeline_started.append((episode_id, start_stage))
        return f"episode-{episode_id}-pipeline"


@pytest_asyncio.fixture
async def api(session_factory):
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeResumeStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def _episode(session_factory, events, *, owner: str | None = None) -> str:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        for event in events:
            await episodes.apply_event(episode.id, event)
        if owner is not None:
            await episodes.set_workflow_id(episode.id, f"episode-{episode.id}-{owner}:run-1")
        await session.commit()
        return episode.id


# --------------------------------------------------------------------- GET /resume/plan


async def test_get_plan_for_storyboard_ready_episode_resumes_at_production(
    api, session_factory
) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.get(f"/episodes/{episode_id}/resume/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["episode_id"] == episode_id
    assert body["resumable"] is True
    assert body["target_stage"] == "production"
    assert body["stages_to_run"] == ["production", "render", "upload"]
    assert body["possible_new_charges"] == ["production", "upload"]
    assert body["unresolved_blockers"] == []
    assert body["reason"] is None
    # dry-run は workflow を一切起動しない（ADR-0032 §Decision(1)）
    assert starter.pipeline_started == []


async def test_get_plan_makes_zero_workflow_starter_calls(api, session_factory) -> None:
    """GET はどんな Episode 状態でも WorkflowStarter に触れない（構造的な保証の検査）。"""
    client, starter = api
    episode_id = await _episode(session_factory, TO_RENDER_READY, owner="render")

    await client.get(f"/episodes/{episode_id}/resume/plan")
    await client.get(f"/episodes/{uuid.uuid4()}/resume/plan")

    assert starter.pipeline_started == []


async def test_get_plan_for_unknown_episode_is_404(api) -> None:
    client, _ = api
    response = await client.get(f"/episodes/{uuid.uuid4()}/resume/plan")
    assert response.status_code == 404


async def test_get_plan_blocked_by_unowned_stage_is_not_resumable(api, session_factory) -> None:
    client, _ = api
    episode_id = await _episode(
        session_factory,
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
        owner="storyboard",  # storyboard は再開対象の工程ではない（所有権が既知の工程と不一致）
    )

    response = await client.get(f"/episodes/{episode_id}/resume/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["target_stage"] is None
    assert body["reason"] is not None and "does not match" in body["reason"]


async def test_get_plan_reports_unreconciled_reservations_as_blockers(api, session_factory) -> None:
    client, _ = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    async with session_factory() as session:
        reservation = await ProviderReservationRepository(session).reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key=f"{episode_id}:fal_image:1",
            input_hash="h1",
            round=1,
            scene_id="scene-1",
        )
        await session.commit()

    response = await client.get(f"/episodes/{episode_id}/resume/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["unreconciled_reservation_ids"] == [reservation.id]
    assert body["reason"] is not None and "unreconciled" in body["reason"]


async def test_get_plan_already_uploaded_is_not_resumable(api, session_factory) -> None:
    client, _ = api
    episode_id = await _episode(
        session_factory,
        [*TO_RENDER_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.UPLOAD_SUCCEEDED],
    )

    response = await client.get(f"/episodes/{episode_id}/resume/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is False
    assert body["stages_to_run"] == []


# ------------------------------------------------------------------------ POST /resume


async def test_post_resume_starts_the_pipeline_workflow_at_the_target_stage(
    api, session_factory
) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["episode_id"] == episode_id
    assert body["workflow_id"] == f"episode-{episode_id}-pipeline"
    assert body["target_stage"] == "production"
    assert body["stages_to_run"] == ["production", "render", "upload"]
    assert starter.pipeline_started == [(episode_id, "production")]


async def test_post_resume_does_not_claim_a_daily_slot_or_touch_other_starter_methods(
    api, session_factory
) -> None:
    """resume は claim_daily_slot も per-stage の start_*_workflow も呼ばない（ADR-0032 §4）。

    FakeResumeStarter の他メソッドは呼ばれたら AssertionError を投げる。202 が返ること自体が
    ``start_pipeline_workflow`` 以外を経由しなかった証拠になる。
    """
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert starter.pipeline_started == [(episode_id, "production")]


async def test_post_resume_from_render_ready_targets_upload(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_RENDER_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 202, response.text
    assert response.json()["target_stage"] == "upload"
    assert starter.pipeline_started == [(episode_id, "upload")]


async def test_post_resume_for_unknown_episode_is_404(api) -> None:
    client, starter = api
    response = await client.post(f"/episodes/{uuid.uuid4()}/resume")
    assert response.status_code == 404
    assert starter.pipeline_started == []


async def test_post_resume_when_not_resumable_is_409_with_the_plan_reason(
    api, session_factory
) -> None:
    client, starter = api
    episode_id = await _episode(
        session_factory,
        [*TO_RENDER_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.UPLOAD_SUCCEEDED],
    )

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    assert "already" in response.json()["detail"].lower()
    assert starter.pipeline_started == []


async def test_post_resume_blocked_by_unresolved_ownership_is_409(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(
        session_factory,
        [*TO_ASSETS_READY, EpisodeEvent.STAGE_ADMITTED, EpisodeEvent.NEEDS_INPUT_FAILURE],
        owner="storyboard",
    )

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    assert "does not match" in response.json()["detail"]
    assert starter.pipeline_started == []


async def test_post_resume_blocked_by_unreconciled_reservation_is_409(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    async with session_factory() as session:
        await ProviderReservationRepository(session).reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key=f"{episode_id}:fal_video:1",
            input_hash="h1",
            round=1,
            scene_id="scene-1",
        )
        await session.commit()

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    assert "unreconciled" in response.json()["detail"]
    assert starter.pipeline_started == []


async def test_post_resume_while_already_running_is_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    response = await client.post(f"/episodes/{episode_id}/resume")

    assert response.status_code == 409, response.text
    assert "already running" in response.json()["detail"]


async def test_a_second_resume_after_the_first_is_already_running_is_409(
    api, session_factory
) -> None:
    """2回目の POST は ``WorkflowAlreadyStartedError`` を 409 に変換する。

    実際の同時実行に対する一意性保証は Temporal の workflow id そのものが持つ
    （``WorkflowIDReusePolicy`` のデフォルトが「実行中の同じ id は拒否」）。ここは
    ``TemporalWorkflowStarter.start_pipeline_workflow`` から ``WorkflowAlreadyStartedError`` が
    上がってきたときにハンドラが正しく 409 へ変換することだけを検査する（fake は直列に模す。
    real Temporal との同時実行検査は integration test の役割）。
    """
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)

    first_response = await client.post(f"/episodes/{episode_id}/resume")
    starter.already_running = True
    second_response = await client.post(f"/episodes/{episode_id}/resume")

    assert first_response.status_code == 202
    assert second_response.status_code == 409
    assert starter.pipeline_started == [(episode_id, "production")]


def test_starter_passes_start_stage_and_no_options() -> None:
    from apps.api.workflow_starter import TemporalWorkflowStarter, pipeline_workflow_id
    from infrastructure.config import Settings

    class _Client:
        def __init__(self) -> None:
            self.calls: list = []

        async def start_workflow(self, name, arg, *, id, task_queue):  # noqa: A002
            self.calls.append((name, arg, id, task_queue))

    client = _Client()
    starter = TemporalWorkflowStarter(client, "q", Settings())  # type: ignore[arg-type]
    asyncio.run(starter.start_pipeline_workflow(episode_id="e", start_stage="production"))
    ((name, arg, wid, queue),) = client.calls
    assert (name, wid, queue) == ("EpisodePipelineWorkflow", pipeline_workflow_id("e"), "pipeline")
    assert arg == {"episode_id": "e", "start_stage": "production"}
