"""介入 command の受付と適用（ADR-0041、``docs/domain/intervention.md`` §2・§4・§5）。

SQLite・InMemoryArtifactStore。Temporal を使わず、command workflow の代わりに
``apply_command`` を直接呼ぶ（Activity の本体と同じ関数）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import func, select

from contracts.intervention import (
    AnnotationKind,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    InterventionMode,
    RejectReason,
    ReviewPoint,
    Scope,
)
from contracts.states import EpisodeStatus, ProviderCall
from infrastructure.db.models import EpisodeEventRow
from infrastructure.db.repositories import ProviderReservationRepository
from infrastructure.intervention.commands import (
    CommandForbiddenError,
    DuplicateCommandError,
    EpisodeNotFoundError,
    InvalidCommandPayloadError,
    Principal,
    record_followup,
)
from infrastructure.intervention.store import ControlChange, InterventionStore
from tests.support.intervention import (
    AI,
    HUMAN,
    apply,
    envelope,
    make_episode,
    receive,
    submit,
)


async def _control(session_factory, episode_id: str):
    async with session_factory() as session:
        return await InterventionStore(session).get_control(episode_id)


async def _event_count(session_factory, episode_id: str) -> int:
    import uuid

    async with session_factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(EpisodeEventRow)
                .where(EpisodeEventRow.episode_id == uuid.UUID(episode_id))
            )
            or 0
        )


async def _events(session_factory, episode_id: str) -> list[InterventionEvent]:
    async with session_factory() as session:
        return [e.event_name for e in await InterventionStore(session).list_events(episode_id)]


# --------------------------------------------------------------------------- 受付


async def test_hold_is_requested_in_the_receiving_transaction_and_not_reported_as_done(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    env = envelope(CommandType.REQUEST_HOLD, {"reason": "check the script"})
    received = await receive(session_factory, episode_id, env)
    assert received.created and received.needs_delivery
    assert received.command.status is CommandStatus.RECEIVED
    assert received.command.granted_scope is Scope.HOLD
    control = await _control(session_factory, episode_id)
    assert control.control_state is ControlState.HOLD_REQUESTED
    assert control.hold_requested_at is not None
    assert await _events(session_factory, episode_id) == [
        InterventionEvent.COMMAND_RECEIVED,
        InterventionEvent.HOLD_REQUESTED,
    ]


async def test_same_id_and_payload_returns_the_existing_command(session, session_factory) -> None:
    """INV-42: 再送は新しい行も適用も作らない。"""
    episode_id = await make_episode(session)
    env = envelope(CommandType.REQUEST_HOLD)
    first = await receive(session_factory, episode_id, env)
    events = await _event_count(session_factory, episode_id)
    again = await receive(session_factory, episode_id, env)
    assert not again.created
    assert again.command.command_id == first.command.command_id
    assert await _event_count(session_factory, episode_id) == events


async def test_same_id_with_another_payload_is_refused(session, session_factory) -> None:
    episode_id = await make_episode(session)
    await receive(
        session_factory, episode_id, envelope(CommandType.REQUEST_HOLD, command_id="c" * 16)
    )
    with pytest.raises(DuplicateCommandError):
        await receive(
            session_factory,
            episode_id,
            envelope(CommandType.REQUEST_HOLD, {"reason": "other"}, command_id="c" * 16),
        )


async def test_receive_refuses_missing_scope_ai_set_mode_bad_payload_and_unknown_episode(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    no_hold = Principal(HUMAN.actor, frozenset({Scope.COMMENT}))
    with pytest.raises(CommandForbiddenError):
        await receive(session_factory, episode_id, envelope(CommandType.REQUEST_HOLD), no_hold)
    ai_with_set_mode = Principal(AI.actor, frozenset(Scope))
    with pytest.raises(CommandForbiddenError):
        await receive(
            session_factory,
            episode_id,
            envelope(CommandType.SET_MODE, {"mode": "co_production"}),
            ai_with_set_mode,
        )
    with pytest.raises(InvalidCommandPayloadError):
        await receive(session_factory, episode_id, envelope(CommandType.COMMENT, {"body": "x"}))
    with pytest.raises(EpisodeNotFoundError):
        await receive(
            session_factory,
            "00000000-0000-0000-0000-000000000000",
            envelope(CommandType.REQUEST_HOLD),
        )
    async with session_factory() as s:
        assert await InterventionStore(s).list_commands(episode_id) == []


async def test_comment_is_applied_at_receipt_and_reapply_returns_the_stored_result(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session)
    env = envelope(CommandType.COMMENT, {"anchor": {"segment_id": "s1"}, "body": "語尾を硬く"})
    received = await receive(session_factory, episode_id, env)
    assert received.command.status is CommandStatus.APPLIED
    assert not received.needs_delivery
    events = await _event_count(session_factory, episode_id)
    outcome = await apply(session_factory, artifact_store, env.command_id)
    assert outcome.replayed and outcome.status is CommandStatus.APPLIED
    assert outcome.result == received.command.result
    assert await _event_count(session_factory, episode_id) == events
    async with session_factory() as s:
        notes = await InterventionStore(s).list_annotations(episode_id)
    assert [(n.kind, n.anchor, n.body) for n in notes] == [
        (AnnotationKind.COMMENT, {"segment_id": "s1"}, "語尾を硬く")
    ]


async def test_user_constraint_is_recorded_with_verifiable_target_ids(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    env = envelope(
        CommandType.SET_USER_CONSTRAINT, {"rule": "keep_segment_text", "target_ids": ["s2"]}
    )
    received = await receive(session_factory, episode_id, env, AI)
    assert received.command.status is CommandStatus.APPLIED
    async with session_factory() as s:
        (note,) = await InterventionStore(s).list_annotations(
            episode_id, kind=AnnotationKind.USER_CONSTRAINT
        )
    assert note.payload == {"rule": "keep_segment_text", "target_ids": ["s2"]}
    assert note.actor == AI.actor


# --------------------------------------------------------------------------- hold


async def test_hold_becomes_held_and_lists_in_flight_unknown_and_undispatched(
    session, session_factory, artifact_store
) -> None:
    """INV-44: HELD は送信済み・成否不明・未送信を列挙する（課金されうるものを隠さない）。"""
    episode_id = await make_episode(session)
    repo = ProviderReservationRepository(session)
    ids = {}
    for name in ("in_flight", "unknown", "undispatched", "spent"):
        ids[name] = (
            await repo.reserve(
                episode_id=episode_id,
                provider=ProviderCall.FAL_IMAGE,
                idempotency_key=f"k-{name}",
                input_hash="h" * 64,
                round=1,
                scene_id=f"sb-{name}",
            )
        ).id
    await session.commit()
    for name in ("in_flight", "unknown", "spent"):
        await repo.mark_dispatched(ids[name])
    await repo.record_provider_job_ref(ids["in_flight"], "fal-secret-request-ref")
    await repo.record_provider_job_ref(ids["spent"], "fal-done")
    await repo.mark_spent(ids["spent"], raw_output_key="provider-raw/x", reconciled_by="evidence")
    await session.commit()

    outcome = await submit(
        session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD)
    )
    assert outcome.status is CommandStatus.APPLIED
    control = await _control(session_factory, episode_id)
    assert control.control_state is ControlState.HELD and control.held_at is not None
    listed = {
        key: [item["reservation_id"] for item in outcome.result[key]]
        for key in ("in_flight", "unknown_outcome", "undispatched")
    }
    assert listed == {
        "in_flight": [ids["in_flight"]],
        "unknown_outcome": [ids["unknown"]],
        "undispatched": [ids["undispatched"]],
    }
    # provider の job 参照（upload では session URI）は応答に出さない（INV-20）
    assert "fal-secret-request-ref" not in json.dumps(outcome.result)
    assert InterventionEvent.HOLD_EFFECTIVE in await _events(session_factory, episode_id)


async def test_applying_the_same_command_twice_is_one_logical_change(
    session, session_factory, artifact_store
) -> None:
    """INV-42: apply の commit 後に結果を失って再実行しても、保存済み result を返すだけ。"""
    episode_id = await make_episode(session)
    env = envelope(CommandType.REQUEST_HOLD)
    first = await submit(session_factory, artifact_store, episode_id, env)
    control = await _control(session_factory, episode_id)
    events = await _event_count(session_factory, episode_id)
    second = await apply(session_factory, artifact_store, env.command_id)
    assert second.replayed and not first.replayed
    assert second.result == first.result
    after = await _control(session_factory, episode_id)
    assert (after.control_revision, after.held_at) == (control.control_revision, control.held_at)
    assert await _event_count(session_factory, episode_id) == events


async def test_a_stale_expected_control_revision_is_a_conflict_and_changes_nothing(
    session, session_factory, artifact_store
) -> None:
    """INV-43: 期待版の不一致は CONFLICT。状態を上書きしない。"""
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    before = await _control(session_factory, episode_id)
    stale = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.RESUME, expected_control_revision=before.control_revision - 1),
    )
    assert stale.status is CommandStatus.CONFLICT
    assert stale.reject_reason is RejectReason.STALE_CONTROL_REVISION
    after = await _control(session_factory, episode_id)
    assert after.control_state is ControlState.HELD
    assert after.control_revision == before.control_revision

    hold = await receive(
        session_factory,
        episode_id,
        envelope(CommandType.REQUEST_HOLD, expected_control_revision=0),
    )
    assert hold.command.status is CommandStatus.CONFLICT


# ------------------------------------------------------------------- resume / release / mode


async def test_resume_runs_again_from_the_next_stage(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    outcome = await submit(
        session_factory, artifact_store, episode_id, envelope(CommandType.RESUME)
    )
    assert outcome.status is CommandStatus.APPLIED
    assert outcome.result["start_stage"] == "script"  # 台本の現行が無い → 台本から
    assert outcome.pipeline_start == {
        "episode_id": episode_id,
        "start_stage": "script",
        "control_gate": False,
    }
    control = await _control(session_factory, episode_id)
    assert control.control_state is ControlState.RUNNING and control.held_at is None

    # follow-up を記録したら、再配送で pipeline を二度 start しない
    await record_followup(
        session_factory, outcome.command_id, {"outcome": "started", "workflow_id": "w"}
    )
    replay = await apply(session_factory, artifact_store, outcome.command_id)
    assert replay.replayed and replay.pipeline_start is None
    assert replay.result["pipeline"] == {"outcome": "started", "workflow_id": "w"}


async def test_resume_is_refused_while_an_unknown_outcome_reservation_exists(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    repo = ProviderReservationRepository(session)
    row = await repo.reserve(
        episode_id=episode_id,
        provider=ProviderCall.CODEX_STORYBOARD,
        idempotency_key="k",
        input_hash="h" * 64,
        round=1,
    )
    await session.commit()
    await repo.mark_dispatched(row.id)
    await session.commit()
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    outcome = await submit(
        session_factory, artifact_store, episode_id, envelope(CommandType.RESUME)
    )
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.reject_reason is RejectReason.INVALID_STATE
    assert (await _control(session_factory, episode_id)).control_state is ControlState.HELD


async def test_release_checkpoint_binds_the_approval_to_the_active_manifest(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    store = InterventionStore(session)
    await store.lock_control(episode_id)
    active = await store.insert_manifest(episode_id, {}, actor=HUMAN.actor, parent_manifest_id=None)
    await store.update_control(
        episode_id,
        ControlChange(
            control_state=ControlState.WAITING_REVIEW,
            active_manifest_id=active.manifest_id,
            waiting_point=ReviewPoint.BEFORE_STORYBOARD,
            waiting_manifest_id=active.manifest_id,
        ),
    )
    old = await store.insert_manifest(episode_id, {}, actor=HUMAN.actor, parent_manifest_id=None)
    await session.commit()

    stale = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(
            CommandType.RELEASE_CHECKPOINT,
            {"point": "before_storyboard", "manifest_id": old.manifest_id},
        ),
    )
    assert stale.status is CommandStatus.CONFLICT
    assert stale.reject_reason is RejectReason.STALE_TARGET_REVISION
    assert (await _control(session_factory, episode_id)).control_state is (
        ControlState.WAITING_REVIEW
    )

    released = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(
            CommandType.RELEASE_CHECKPOINT,
            {"point": "before_storyboard", "manifest_id": active.manifest_id},
        ),
    )
    assert released.status is CommandStatus.APPLIED
    assert released.pipeline_start == {
        "episode_id": episode_id,
        "start_stage": "storyboard",
        "control_gate": True,
    }
    control = await _control(session_factory, episode_id)
    assert control.control_state is ControlState.RUNNING and control.waiting_point is None
    async with session_factory() as s:
        (approval,) = await InterventionStore(s).list_annotations(
            episode_id, kind=AnnotationKind.APPROVAL
        )
    assert approval.manifest_id == active.manifest_id
    assert approval.payload == {"point": "before_storyboard"}


async def test_set_mode_by_a_human_uses_the_default_co_production_points(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session)
    outcome = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.SET_MODE, {"mode": "co_production"}),
    )
    assert outcome.status is CommandStatus.APPLIED
    control = await _control(session_factory, episode_id)
    assert control.mode is InterventionMode.CO_PRODUCTION
    assert control.review_points == (
        ReviewPoint.BEFORE_STORYBOARD,
        ReviewPoint.BEFORE_PRODUCTION,
        ReviewPoint.BEFORE_UPLOAD,
    )


async def test_cancel_is_recorded_at_receipt_and_cannot_be_resumed(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    received = await receive(session_factory, episode_id, envelope(CommandType.REQUEST_CANCEL))
    assert received.command.status is CommandStatus.APPLIED
    assert (await _control(session_factory, episode_id)).control_state is (
        ControlState.CANCEL_REQUESTED
    )
    resume = await submit(session_factory, artifact_store, episode_id, envelope(CommandType.RESUME))
    assert resume.reject_reason is RejectReason.INVALID_STATE


async def test_the_recorded_scope_is_the_one_the_command_type_requires(
    session, session_factory
) -> None:
    """command 行の ``granted_scope`` は principal の全 scope ではなく、型が要求する scope。"""
    from contracts.intervention import REQUIRED_SCOPE

    episode_id = await make_episode(session)
    payloads = {
        CommandType.COMMENT: {"anchor": {}, "body": "x"},
        CommandType.SET_USER_CONSTRAINT: {"rule": "keep_scene_visual", "target_ids": ["sb1"]},
        CommandType.REQUEST_HOLD: {},
        CommandType.RESUME: {},
        CommandType.SET_MODE: {"mode": "legacy"},
        CommandType.REJECT_CHANGE: {"change_set_id": "00000000-0000-0000-0000-000000000000"},
    }
    for command_type, payload in payloads.items():
        received = await receive(session_factory, episode_id, envelope(command_type, payload))
        assert received.command.granted_scope is REQUIRED_SCOPE[command_type], command_type
