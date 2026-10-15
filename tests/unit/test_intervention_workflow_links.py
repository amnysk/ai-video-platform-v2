"""判断記録・実行記録と Temporal / Scene の結び付き（migration 0018、JP-MAS-001）。

SQLite + InMemoryArtifactStore + ``ActivityEnvironment``（Temporal server は不要）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from temporalio.testing import ActivityEnvironment

from contracts.intervention import (
    AgentRole,
    AgentRunOutcome,
    CommandStatus,
    InterventionEvent,
)
from infrastructure.db.models import AgentRunRow, DecisionRecordRow
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.capture import AgentRunRecorder, PromptSource, RunCapture
from infrastructure.intervention.projection import event_fields
from infrastructure.intervention.records import (
    generation_decision_payload,
    record_generation_decision,
)
from infrastructure.intervention.store import InterventionStore
from infrastructure.intervention.temporal_context import current_activity_ids
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.unit.test_intervention_changes import (
    RATE,
    _apply_change,
    _override,
    _propose,
    _script_edit,
    _setup,
)


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


def _capture(episode_id: str, key: str) -> RunCapture:
    return RunCapture(
        idempotency_key=key,
        role=AgentRole.WRITER_DIRECTOR,
        step="write_script",
        model="m",
        prompt_version="v@1",
        prompt="P",
        prompt_source=PromptSource.CAPTURED,
        input_refs={},
        request=None,
        response=None,
        episode_id=episode_id,
    )


async def _record_and_decide(session, episode_id: str, key: str):
    run = await AgentRunRecorder(InMemoryArtifactStore()).record(session, _capture(episode_id, key))
    payload = generation_decision_payload(
        decision_type="write_script",
        question="q",
        candidate_summary="s",
        result_revision=str(uuid.uuid4()),
        input_snapshot_ref=None,
    )
    decision = await record_generation_decision(
        session, episode_id=episode_id, agent_run=run, payload=payload
    )
    await session.commit()
    return run, decision


async def _events(session, episode_id: str, name: InterventionEvent):
    return [
        e for e in await InterventionStore(session).list_events(episode_id) if e.event_name is name
    ]


async def test_outside_an_activity_there_are_no_temporal_ids_and_nothing_is_invented(
    session,
) -> None:
    assert current_activity_ids() is None
    episode_id = await _episode(session)

    run, decision = await _record_and_decide(session, episode_id, "k-outside")

    run_row = await session.get(AgentRunRow, uuid.UUID(run.agent_run_id))
    assert run_row is not None
    assert (run_row.workflow_id, run_row.workflow_run_id, run_row.activity_id) == (None,) * 3
    decision_row = await session.get(DecisionRecordRow, uuid.UUID(decision.decision_id))
    assert decision_row is not None
    assert (decision_row.workflow_id, decision_row.workflow_run_id, decision_row.scene_id) == (
        None,
    ) * 3
    for name in (InterventionEvent.AGENT_RUN_RECORDED, InterventionEvent.DECISION_RECORDED):
        (event,) = await _events(session, episode_id, name)
        assert not {"workflow_id", "run_id", "activity_id"} & set(event.refs)


async def test_inside_an_activity_the_run_the_decision_and_their_events_carry_the_ids(
    session,
) -> None:
    episode_id = await _episode(session)

    run, decision = await ActivityEnvironment().run(
        _record_and_decide, session, episode_id, "k-inside"
    )

    assert (run.workflow_id, run.workflow_run_id, run.activity_id) == ("test", "test-run", "test")
    assert (decision.workflow_id, decision.workflow_run_id) == ("test", "test-run")
    assert decision.scene_id is None  # Scene は payload が明示したときだけ
    for name in (InterventionEvent.AGENT_RUN_RECORDED, InterventionEvent.DECISION_RECORDED):
        (event,) = await _events(session, episode_id, name)
        assert event.refs["workflow_id"] == "test" and event.refs["run_id"] == "test-run"
        # 既存のログ項目（log_contract の workflow_id / run_id）へ載る。新しい項目は作らない
        fields = event_fields(event)
        assert fields["workflow_id"] == "test" and fields["run_id"] == "test-run"


async def test_a_scene_override_decision_is_linked_to_its_scene_and_a_script_edit_is_not(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, storyboard, _media = await _setup(session, artifact_store)
    edit = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    await _apply_change(
        session_factory, artifact_store, episode_id, edit.result["change_set_id"], policy=None
    )
    override = await _propose(
        session_factory, artifact_store, episode_id, _override(storyboard.id, "sb2")
    )
    applied = await _apply_change(
        session_factory, artifact_store, episode_id, override.result["change_set_id"], policy=RATE
    )
    assert applied.status is CommandStatus.APPLIED, applied.result

    async with session_factory() as s:
        rows = list(
            await s.scalars(
                select(DecisionRecordRow).where(DecisionRecordRow.decision_type == "apply_change")
            )
        )
    by_scene = sorted(r.scene_id or "" for r in rows)
    # segment 単位の台本編集は Scene を特定しない（NULL）。scene.override_visual は scene_id を持つ
    assert by_scene == ["", "sb2"]


async def test_the_read_model_and_the_review_packet_list_the_links(
    session_factory, artifact_store
) -> None:
    from contracts.intervention import ActorType
    from infrastructure.intervention.read_model import EpisodeReadModel
    from infrastructure.intervention.review_packet import _decision
    from infrastructure.intervention.store import Actor
    from tests.support.intervention import make_episode

    async with session_factory() as session:
        episode_id = await make_episode(session)
        await session.commit()

    async def insert(store: InterventionStore):
        run = await store.insert_agent_run(
            idempotency_key="rm-run",
            role=AgentRole.WRITER_DIRECTOR,
            step="write_script",
            snapshot_object_key="agent-runs/x/c.json",
            snapshot_sha256="a" * 64,
            outcome=AgentRunOutcome.SUCCEEDED,
            episode_id=episode_id,
        )
        await store.insert_decision(
            idempotency_key="rm-decision",
            decision_type="apply_change",
            actor=Actor(ActorType.AGENT, "writer_director"),
            payload={"decision_type": "apply_change"},
            episode_id=episode_id,
            agent_run_id=run.agent_run_id,
            scene_id="sb2",
        )

    async with session_factory() as session:
        await ActivityEnvironment().run(insert, InterventionStore(session))
        await session.commit()

    view = await EpisodeReadModel(session_factory, artifact_store).load(episode_id)
    assert view is not None
    (decision,) = [d for d in view.decisions if d.decision_type == "apply_change"]
    assert (decision.scene_id, decision.workflow_id, decision.workflow_run_id) == (
        "sb2",
        "test",
        "test-run",
    )
    (run_view,) = view.agent_runs
    assert (run_view.workflow_id, run_view.workflow_run_id, run_view.activity_id) == (
        "test",
        "test-run",
        "test",
    )
    packet = _decision(decision)
    assert (packet.scene_id, packet.workflow_id, packet.workflow_run_id) == (
        "sb2",
        "test",
        "test-run",
    )
