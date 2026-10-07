"""dispatch gate（ADR-0041 §2 / INV-44 / INV-46）。SQLite で実行する。

PostgreSQL の行 lock での hold と dispatch の競合は
``tests/integration/test_intervention_gate_race.py``（隔離環境で D が実行）。
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from contracts.intervention import ActorType, ControlState, InterventionMode
from contracts.states import ArtifactType, ProviderCall, ReservationStatus
from domain.errors import EpisodeHeldError, MissingDecisionRecordError
from domain.production.ports import ImageRequest
from infrastructure.db.models import EpisodeControlRow, ProviderReservationRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.store import Actor, ControlChange, InterventionStore
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator

HUMAN = Actor(ActorType.HUMAN, "owner")


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


async def _reserve(session, episode_id: str, *, provider=ProviderCall.FAL_IMAGE, key="k1"):
    row = await ProviderReservationRepository(session).reserve(
        episode_id=episode_id,
        provider=provider,
        idempotency_key=key,
        input_hash="h" * 64,
        round=1,
        scene_id="sb1" if provider is ProviderCall.FAL_IMAGE else None,
    )
    await session.commit()
    return row


async def _set_control(session, episode_id: str, change: ControlChange) -> None:
    store = InterventionStore(session)
    await store.lock_control(episode_id)
    await store.update_control(episode_id, change)
    await session.commit()


async def test_an_episode_without_a_control_row_dispatches_and_nothing_is_written(
    session,
) -> None:
    episode_id = await _episode(session)
    row = await _reserve(session, episode_id)
    dispatched = await ProviderReservationRepository(session).mark_dispatched(row.id)
    await session.commit()
    assert dispatched.dispatched_at is not None
    count = await session.scalar(select(func.count()).select_from(EpisodeControlRow))
    assert count == 0


@pytest.mark.parametrize(
    "state",
    [
        ControlState.HOLD_REQUESTED,
        ControlState.HELD,
        ControlState.WAITING_REVIEW,
        ControlState.CANCEL_REQUESTED,
    ],
)
async def test_a_non_running_episode_refuses_dispatch_and_leaves_the_reservation_unsent(
    session, state: ControlState
) -> None:
    """INV-44: 予約は reserved かつ未 dispatch のまま（送っていない証拠）。"""
    episode_id = await _episode(session)
    row = await _reserve(session, episode_id)
    await _set_control(session, episode_id, ControlChange(control_state=state))
    with pytest.raises(EpisodeHeldError):
        await ProviderReservationRepository(session).mark_dispatched(row.id)
    await session.rollback()
    fresh = await session.get(ProviderReservationRow, uuid.UUID(row.id), populate_existing=True)
    assert fresh is not None
    assert fresh.status == ReservationStatus.RESERVED.value
    assert fresh.dispatched_at is None


async def test_upload_dispatch_is_refused_while_held_but_an_in_flight_session_is_not_reopened(
    session,
) -> None:
    episode_id = await _episode(session)
    repo = ProviderReservationRepository(session)
    first = await _reserve(session, episode_id, provider=ProviderCall.YOUTUBE_UPLOAD, key="u1")
    assert await repo.record_upload_session(first.id, "s1")
    assert await repo.mark_upload_dispatched(first.id, "s1") == (True, True)
    second = await _reserve(session, episode_id, provider=ProviderCall.YOUTUBE_UPLOAD, key="u2")
    assert await repo.record_upload_session(second.id, "s2")
    await session.commit()

    await _set_control(session, episode_id, ControlChange(control_state=ControlState.HELD))
    # 送信済みの session の再入（bytes の続き）は新しい dispatch ではない（in-flight に列挙）
    assert await repo.mark_upload_dispatched(first.id, "s1") == (True, False)
    with pytest.raises(EpisodeHeldError):
        await repo.mark_upload_dispatched(second.id, "s2")
    await session.rollback()
    fresh = await session.get(ProviderReservationRow, uuid.UUID(second.id), populate_existing=True)
    assert fresh is not None and fresh.dispatched_at is None


async def _record(session, episode_id: str, artifact_type: ArtifactType, sha: str):
    meta = await ArtifactMetadataRepository(session).record(
        episode_id=episode_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        bucket="b",
        object_key=f"artifacts/{episode_id}/{artifact_type.value}/{sha}.json",
        sha256=sha,
    )
    await session.commit()
    return meta


async def test_new_mode_requires_a_record_for_the_current_script_and_storyboard(session) -> None:
    """INV-46: mode != legacy では、記録の無い現行 script / storyboard の上で dispatch しない。"""
    episode_id = await _episode(session)
    await _set_control(session, episode_id, ControlChange(mode=InterventionMode.AUTO_INTERVENABLE))
    script = await _record(session, episode_id, ArtifactType.SCRIPT, "1" * 64)
    storyboard = await _record(session, episode_id, ArtifactType.STORYBOARD, "2" * 64)
    row = await _reserve(session, episode_id)
    repo = ProviderReservationRepository(session)
    with pytest.raises(MissingDecisionRecordError) as err:
        await repo.mark_dispatched(row.id)
    assert "script" in str(err.value) and "storyboard" in str(err.value)
    await session.rollback()

    store = InterventionStore(session)
    await store.insert_decision(
        idempotency_key="d-script",
        decision_type="human_edit",
        actor=HUMAN,
        payload={},
        episode_id=episode_id,
        result_revision_id=script.id,
    )
    await session.commit()
    with pytest.raises(MissingDecisionRecordError) as err:
        await repo.mark_dispatched(row.id)
    assert "storyboard" in str(err.value) and "script" not in str(err.value)
    await session.rollback()

    from contracts.intervention import AgentRole, AgentRunOutcome

    await store.insert_agent_run(
        idempotency_key="run-sb",
        role=AgentRole.WRITER_DIRECTOR,
        step="storyboard",
        snapshot_object_key="agent-runs/x/context.json",
        snapshot_sha256="3" * 64,
        outcome=AgentRunOutcome.SUCCEEDED,
        episode_id=episode_id,
        output_revision_id=storyboard.id,
    )
    await session.commit()
    dispatched = await repo.mark_dispatched(row.id)
    assert dispatched.dispatched_at is not None


async def test_legacy_mode_with_a_row_does_not_require_records(session) -> None:
    episode_id = await _episode(session)
    await _set_control(session, episode_id, ControlChange(review_points=()))
    await _record(session, episode_id, ArtifactType.SCRIPT, "1" * 64)
    row = await _reserve(session, episode_id)
    dispatched = await ProviderReservationRepository(session).mark_dispatched(row.id)
    assert dispatched.dispatched_at is not None


async def test_a_reservation_refused_by_the_gate_is_reused_after_resume(
    session_factory, artifact_store, tmp_path
) -> None:
    """INV-44: hold で止めた予約は resume 後に同じ冪等キーで再利用される（二重予約しない）。"""
    async with session_factory() as session:
        episode_id = await _episode(session)
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )
    spec = PaidJobSpec(
        episode_id=episode_id,
        scene_id="sb1",
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash="h" * 64,
        round=1,
    )
    request = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")
    async with session_factory() as session:
        await _set_control(
            session, episode_id, ControlChange(control_state=ControlState.HOLD_REQUESTED)
        )
    gen = FakeImageGenerator()
    with pytest.raises(EpisodeHeldError):
        await runner.submit(spec, gen, request)
    assert gen.submit_calls == 0
    async with session_factory() as session:
        rows = (await session.scalars(select(ProviderReservationRow))).all()
        assert len(rows) == 1
        held_id = rows[0].id
        assert rows[0].status == ReservationStatus.RESERVED.value
        assert rows[0].dispatched_at is None

    async with session_factory() as session:
        await _set_control(
            session,
            episode_id,
            ControlChange(control_state=ControlState.RUNNING, clear_hold_times=True),
        )
    outcome = await runner.submit(spec, gen, request)
    assert isinstance(outcome, Submitted)
    assert outcome.reservation_id == str(held_id)
    assert gen.submit_calls == 1
    async with session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ProviderReservationRow))
    assert count == 1
