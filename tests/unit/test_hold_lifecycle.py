"""信用保留（credit hold）の寿命（review-E P1-1）。

保留は resume CLI でしか解放されない。それ以外の経路（API 再開・完了・取消）で Episode が
保留状態を離れても行が残り、``open_hold_count`` が MAX_HELD_EPISODES を永久に満たして
日次が HELD_BACKLOG のまま止まる。読み取り側は Episode が今も保留状態にある行だけを数え、
API 再開は保留が開いている間は拒否する（解放は承認・予約照合・予算を検査する CLI だけが行う）。
"""

from __future__ import annotations

import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from contracts.provider_health import HoldReason, ProviderAccount, ProviderFamily
from contracts.states import EpisodeStatus
from infrastructure.db.models import EpisodeRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository
from tests.unit.test_resume_api import TO_STORYBOARD_READY, FakeResumeStarter

FAL = ProviderAccount(ProviderFamily.FAL)


async def _held(session_factory, health, *, status: str) -> str:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        ep = await episodes.create(topic="t")
        for event in TO_STORYBOARD_READY:
            await episodes.apply_event(ep.id, event)
        await session.commit()
    await health.hold_episode(
        episode_id=str(ep.id),
        scene_id=None,
        account=FAL,
        reason=HoldReason.WAITING_FOR_CREDITS,
        resumable_stage="production",
        generation_profile_id=None,
    )
    await _set_status(session_factory, ep.id, status)
    return str(ep.id)


async def _set_status(session_factory, ep, status: str) -> None:
    async with session_factory() as session:
        await session.execute(
            update(EpisodeRow).where(EpisodeRow.id == uuid.UUID(str(ep))).values(status=status)
        )
        await session.commit()


async def test_only_episodes_still_in_a_held_state_count(session_factory) -> None:
    health = PostgresProviderHealth(session_factory)
    live = await _held(session_factory, health, status=EpisodeStatus.BLOCKED.value)
    daily = await _held(session_factory, health, status=EpisodeStatus.STORYBOARD_READY.value)
    for status in ("completed", "cancelled", "failed", "uploaded", "in_progress"):
        await _held(session_factory, health, status=status)

    assert await health.open_hold_count() == 2
    assert {h.episode_id for h in await health.list_open_holds()} == {live, daily}
    assert set(await health.held_episode_ids_oldest_first(account=FAL, limit=10)) == {live, daily}


async def test_stale_holds_do_not_trigger_the_held_backlog_cap(session_factory) -> None:
    from contracts.provider_health import MAX_HELD_EPISODES

    health = PostgresProviderHealth(session_factory)
    for _ in range(MAX_HELD_EPISODES):
        await _held(session_factory, health, status="completed")
    assert await health.open_hold_count() < MAX_HELD_EPISODES


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


async def test_api_resume_refuses_while_a_credit_hold_is_open(api, session_factory) -> None:
    client, starter = api
    health = PostgresProviderHealth(session_factory)
    ep = await _held(session_factory, health, status=EpisodeStatus.STORYBOARD_READY.value)

    response = await client.post(f"/episodes/{ep}/resume")

    assert response.status_code == 409, response.text
    assert "provider-credits" in response.json()["detail"]
    assert starter.pipeline_started == []
    assert await health.open_hold_count() == 1  # 解放は CLI だけ（保留は触らない）


async def test_api_resume_works_once_the_cli_released_the_hold(api, session_factory) -> None:
    client, starter = api
    health = PostgresProviderHealth(session_factory)
    ep = await _held(session_factory, health, status=EpisodeStatus.STORYBOARD_READY.value)
    await health.release_holds(ep, account=FAL, approval_fingerprint="OWNER-20260101-x")

    response = await client.post(f"/episodes/{ep}/resume")

    assert response.status_code == 202, response.text
    assert starter.pipeline_started == [(ep, "production")]


async def test_api_resume_ignores_a_stale_hold_of_a_finished_episode(api, session_factory) -> None:
    client, _ = api
    health = PostgresProviderHealth(session_factory)
    ep = await _held(session_factory, health, status=EpisodeStatus.STORYBOARD_READY.value)
    # 保留行は残るが、Episode が保留状態を離れていれば再開の可否は従来の計画だけで決まる
    await _set_status(session_factory, ep, "completed")
    response = await client.post(f"/episodes/{ep}/resume")
    assert response.status_code == 409
    assert "provider-credits" not in response.json()["detail"]
