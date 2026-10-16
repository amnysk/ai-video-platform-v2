"""Pipeline Activity（ADR-0023）。SQLite（インメモリ）+ 実 repository。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import update

from contracts.operations import ClaimOutcome, OperationalSwitch
from contracts.pipeline import (
    CheckPausedRequest,
    ClaimDailySlotRequest,
    UploadGateRequest,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    OperationalSwitchRepository,
    ProviderReservationRepository,
    TopicPlanRepository,
)
from workers.pipeline.activities import PipelineActivities


def _acts(session_factory: Any, **kw: Any) -> PipelineActivities:
    return PipelineActivities(
        session_factory=session_factory,
        paused_env=kw.get("paused_env", False),
        uploads_paused_env=kw.get("uploads_paused_env", False),
    )


async def _set_switch(session_factory: Any, switch: OperationalSwitch) -> None:
    async with session_factory() as s:
        await OperationalSwitchRepository(s).set(switch, True, reason="test")
        await s.commit()


async def _episode(session_factory: Any, status: EpisodeStatus, *, final_video: bool = True) -> str:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic="t")
        await s.execute(
            update(EpisodeRow).where(EpisodeRow.id == uuid.UUID(ep.id)).values(status=status.value)
        )
        if final_video:
            await ArtifactMetadataRepository(s).record(
                episode_id=ep.id,
                artifact_type=ArtifactType.FINAL_VIDEO,
                schema_version="1.0",
                bucket="artifacts",
                object_key=f"episodes/{ep.id}/final.mp4",
                sha256="a" * 64,
                input_hash="h" * 64,
            )
        await s.commit()
        return ep.id


# ------------------------------------------------------------------------- check_paused


@pytest.mark.asyncio
async def test_check_paused_env_or_db(session_factory) -> None:
    assert (await _acts(session_factory).check_paused(CheckPausedRequest())).paused is False
    assert (
        await _acts(session_factory, paused_env=True).check_paused(CheckPausedRequest())
    ).paused is True
    # uploads_paused は Daily（include_uploads=False）を止めない
    assert (
        await _acts(session_factory, uploads_paused_env=True).check_paused(CheckPausedRequest())
    ).paused is False
    await _set_switch(session_factory, OperationalSwitch.PAUSED)
    result = await _acts(session_factory).check_paused(CheckPausedRequest())
    assert result.paused is True and result.reason


@pytest.mark.asyncio
async def test_check_paused_uploads_via_db(session_factory) -> None:
    await _set_switch(session_factory, OperationalSwitch.UPLOADS_PAUSED)
    acts = _acts(session_factory)
    assert (await acts.check_paused(CheckPausedRequest())).paused is False
    assert (await acts.check_paused(CheckPausedRequest(include_uploads=True))).paused is True


# ------------------------------------------------------------------------- claim


@pytest.mark.asyncio
async def test_claim_commits_and_is_idempotent_per_trigger(session_factory) -> None:
    acts = _acts(session_factory)
    req = ClaimDailySlotRequest(slot_date="2026-09-16", trigger_id="daily-episode-1", daily_limit=1)
    first = await acts.claim_daily_slot(req)
    assert first.outcome == ClaimOutcome.CREATED and first.episode_id
    again = await acts.claim_daily_slot(req)
    assert again.outcome == ClaimOutcome.EXISTING
    assert again.episode_id == first.episode_id


@pytest.mark.asyncio
async def test_claim_three_triggers_same_day_limit_one(session_factory) -> None:
    acts = _acts(session_factory)
    outcomes = []
    for i in range(3):
        r = await acts.claim_daily_slot(
            ClaimDailySlotRequest(slot_date="2026-09-16", trigger_id=f"t{i}", daily_limit=1)
        )
        outcomes.append(r)
        if r.outcome == ClaimOutcome.CREATED:
            # pipeline が走り始めた（planned を出た）ことにする
            async with session_factory() as s:
                await s.execute(
                    update(EpisodeRow)
                    .where(EpisodeRow.id == uuid.UUID(r.episode_id))
                    .values(status=EpisodeStatus.IN_PROGRESS.value)
                )
                await s.commit()
    assert [o.outcome for o in outcomes] == [
        ClaimOutcome.CREATED,
        ClaimOutcome.LIMIT_REACHED,
        ClaimOutcome.LIMIT_REACHED,
    ]
    # 翌日は別枠
    next_day = await acts.claim_daily_slot(
        ClaimDailySlotRequest(slot_date="2026-09-17", trigger_id="t-next", daily_limit=1)
    )
    assert next_day.outcome == ClaimOutcome.CREATED


@pytest.mark.asyncio
async def test_claim_resumes_planned_episode_after_crash(session_factory) -> None:
    acts = _acts(session_factory)
    first = await acts.claim_daily_slot(
        ClaimDailySlotRequest(slot_date="2026-09-16", trigger_id="crashed", daily_limit=1)
    )
    resumed = await acts.claim_daily_slot(
        ClaimDailySlotRequest(slot_date="2026-09-16", trigger_id="next", daily_limit=1)
    )
    assert resumed.outcome == ClaimOutcome.RESUME
    assert resumed.episode_id == first.episode_id


async def _plan(session_factory: Any, day: str = "2026-09-16") -> str:
    from datetime import date

    from tests.unit.test_topic_plan_repositories import candidates, new_plan

    async with session_factory() as s:
        saved = await TopicPlanRepository(s).save_plan(
            new_plan(day=date.fromisoformat(day)), candidates(), selected_ordinal=1
        )
        await s.commit()
        return saved.plan.id


@pytest.mark.asyncio
async def test_claim_carries_topic_plan_id_created_and_existing(session_factory) -> None:
    plan_id = await _plan(session_factory)
    acts = _acts(session_factory)
    req = ClaimDailySlotRequest(
        slot_date="2026-09-16",
        trigger_id="daily-1",
        daily_limit=1,
        topic="Why samurai wore two swords",
        topic_plan_id=plan_id,
    )
    first = await acts.claim_daily_slot(req)
    assert first.outcome == ClaimOutcome.CREATED and first.topic_plan_id == plan_id
    again = await acts.claim_daily_slot(req)
    assert again.outcome == ClaimOutcome.EXISTING
    assert again.episode_id == first.episode_id and again.topic_plan_id == plan_id


@pytest.mark.asyncio
async def test_claim_resume_attaches_plan_to_planless_episode(session_factory) -> None:
    acts = _acts(session_factory)
    crashed = await acts.claim_daily_slot(
        ClaimDailySlotRequest(slot_date="2026-09-16", trigger_id="old", daily_limit=1)
    )
    assert crashed.topic_plan_id is None
    plan_id = await _plan(session_factory)
    resumed = await acts.claim_daily_slot(
        ClaimDailySlotRequest(
            slot_date="2026-09-16",
            trigger_id="new",
            daily_limit=1,
            topic="Why samurai wore two swords",
            topic_plan_id=plan_id,
        )
    )
    assert resumed.outcome == ClaimOutcome.RESUME
    assert resumed.episode_id == crashed.episode_id and resumed.topic_plan_id == plan_id


@pytest.mark.asyncio
async def test_claim_limit_reached_has_no_topic_plan(session_factory) -> None:
    acts = _acts(session_factory)
    plan_id = await _plan(session_factory)
    first = await acts.claim_daily_slot(
        ClaimDailySlotRequest("2026-09-16", "a", 1, topic="x", topic_plan_id=plan_id)
    )
    async with session_factory() as s:
        await s.execute(
            update(EpisodeRow)
            .where(EpisodeRow.id == uuid.UUID(first.episode_id or ""))
            .values(status=EpisodeStatus.IN_PROGRESS.value)
        )
        await s.commit()
    limited = await acts.claim_daily_slot(
        ClaimDailySlotRequest("2026-09-16", "b", 2, topic="x", topic_plan_id=plan_id)
    )
    assert limited.outcome == ClaimOutcome.LIMIT_REACHED
    assert limited.episode_id is None and limited.topic_plan_id is None


# ------------------------------------------------------------------------- upload gate


@pytest.mark.asyncio
async def test_gate_allows_render_ready_with_final_video(session_factory) -> None:
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY)
    result = await _acts(session_factory).upload_gate(UploadGateRequest(ep))
    assert result.allowed is True
    assert result.status == "render_ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["paused_env", "uploads_paused_env"])
async def test_gate_refuses_when_env_paused(session_factory, env: str) -> None:
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY)
    result = await _acts(session_factory, **{env: True}).upload_gate(UploadGateRequest(ep))
    assert result.allowed is False and result.reason


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", list(OperationalSwitch))
async def test_gate_refuses_when_db_switch_on(session_factory, switch: OperationalSwitch) -> None:
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY)
    await _set_switch(session_factory, switch)
    result = await _acts(session_factory).upload_gate(UploadGateRequest(ep))
    assert result.allowed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [EpisodeStatus.BLOCKED, EpisodeStatus.UPLOADED, EpisodeStatus.ASSETS_READY]
)
async def test_gate_refuses_when_not_render_ready(session_factory, status: EpisodeStatus) -> None:
    ep = await _episode(session_factory, status)
    result = await _acts(session_factory).upload_gate(UploadGateRequest(ep))
    assert result.allowed is False
    assert result.status == status.value


@pytest.mark.asyncio
async def test_gate_refuses_missing_episode_or_final_video(session_factory) -> None:
    acts = _acts(session_factory)
    missing = await acts.upload_gate(UploadGateRequest("00000000-0000-0000-0000-000000000000"))
    assert missing.allowed is False and missing.status == ""
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY, final_video=False)
    result = await acts.upload_gate(UploadGateRequest(ep))
    assert result.allowed is False and "final_video" in (result.reason or "")


async def _reserve(session_factory: Any, ep: str, *, dispatch: bool, spend: bool) -> None:
    async with session_factory() as s:
        repo = ProviderReservationRepository(s)
        r = await repo.reserve(
            episode_id=ep,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=f"upload:{ep}",
            input_hash="h" * 64,
            round=1,
        )
        await s.commit()
        if dispatch:
            await repo.mark_dispatched(r.id)
            await s.commit()
        if spend:
            spent = await repo.mark_spent(r.id, raw_output_key=None)
            assert spent.status == ReservationStatus.SPENT
            await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(("dispatch", "spend"), [(True, False), (True, True)])
async def test_gate_refuses_when_upload_reservation_dispatched_or_spent(
    session_factory, dispatch: bool, spend: bool
) -> None:
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY)
    await _reserve(session_factory, ep, dispatch=dispatch, spend=spend)
    result = await _acts(session_factory).upload_gate(UploadGateRequest(ep))
    assert result.allowed is False
    assert "reservation" in (result.reason or "")


@pytest.mark.asyncio
async def test_gate_allows_undispatched_reservation(session_factory) -> None:
    """未 dispatch の予約は「呼んでいない」ことが確定している。UploadWorkflow が再利用する。"""
    ep = await _episode(session_factory, EpisodeStatus.RENDER_READY)
    await _reserve(session_factory, ep, dispatch=False, spend=False)
    result = await _acts(session_factory).upload_gate(UploadGateRequest(ep))
    assert result.allowed is True
