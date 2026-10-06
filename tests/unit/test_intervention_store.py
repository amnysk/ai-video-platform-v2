"""InterventionStore（ADR-0041）の永続化の基本性質。SQLite で実行する。

PostgreSQL の行 lock・並行性は tests/integration（dispatch gate と hold の競合）で検査する。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeKind,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    InterventionMode,
    RevisionLinkKind,
    Scope,
)
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention import store as store_module
from infrastructure.intervention.store import (
    Actor,
    ControlChange,
    DuplicateCommandError,
    InterventionStore,
)

HUMAN = Actor(ActorType.HUMAN, "owner")


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


async def test_an_episode_without_a_row_reads_as_legacy_running_without_writing(session) -> None:
    episode_id = await _episode(session)
    control = await InterventionStore(session).get_control(episode_id)
    assert control.persisted is False
    assert control.mode is InterventionMode.LEGACY
    assert control.control_state is ControlState.RUNNING
    await session.commit()
    assert (await InterventionStore(session).get_control(episode_id)).persisted is False


async def test_lock_control_creates_the_row_once_and_update_bumps_the_revision(session) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    first = await store.lock_control(episode_id)
    again = await store.lock_control(episode_id)
    assert first.persisted and again.persisted
    updated = await store.update_control(
        episode_id, ControlChange(control_state=ControlState.HOLD_REQUESTED)
    )
    await session.commit()
    assert updated.control_state is ControlState.HOLD_REQUESTED
    assert updated.control_revision == 1


async def test_event_seq_is_monotonic_per_episode(session) -> None:
    episode_id = await _episode(session)
    other = await _episode(session)
    store = InterventionStore(session)
    a = await store.append_event(episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {"x": 1})
    b = await store.append_event(episode_id, InterventionEvent.HOLD_REQUESTED, HUMAN, {})
    c = await store.append_event(other, InterventionEvent.COMMENT_ADDED, HUMAN, {})
    await session.commit()
    assert (a.seq, b.seq, c.seq) == (1, 2, 1)
    assert [e.seq for e in await store.list_events(episode_id)] == [1, 2]


async def test_same_command_id_same_payload_returns_the_existing_row(session) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    kwargs: dict[str, Any] = dict(
        command_id="cmd-00000001",
        episode_id=episode_id,
        type=CommandType.COMMENT,
        actor=HUMAN,
        granted_scope=Scope.COMMENT,
        expected_control_revision=None,
        expected_target_revision=None,
        payload={"body": "x"},
        payload_sha256="a" * 64,
        correlation_id=None,
    )
    first, created = await store.insert_command(**kwargs)
    await session.commit()
    second, created_again = await store.insert_command(**kwargs)
    assert created and not created_again
    assert second.command_id == first.command_id
    assert second.status is CommandStatus.RECEIVED
    with pytest.raises(DuplicateCommandError):
        await store.insert_command(**{**kwargs, "payload_sha256": "b" * 64})


async def test_manifests_are_numbered_and_links_are_idempotent(session, artifact_store) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    m1 = await store.insert_manifest(
        episode_id, {"script": "r1"}, actor=HUMAN, parent_manifest_id=None
    )
    m2 = await store.insert_manifest(
        episode_id, {"script": "r2"}, actor=HUMAN, parent_manifest_id=m1.manifest_id
    )
    assert (m1.seq, m2.seq) == (1, 2)

    artifacts = ArtifactMetadataRepository(session)
    from contracts.states import ArtifactType

    up = await artifacts.record(
        episode_id=episode_id,
        artifact_type=ArtifactType.SCRIPT,
        schema_version="1.0",
        bucket="b",
        object_key="k1",
        sha256="1" * 64,
        size_bytes=1,
    )
    down = await artifacts.record(
        episode_id=episode_id,
        artifact_type=ArtifactType.STORYBOARD,
        schema_version="1.0",
        bucket="b",
        object_key="k2",
        sha256="2" * 64,
        size_bytes=1,
    )
    for _ in range(2):
        await store.add_link(episode_id, up.id, down.id, RevisionLinkKind.DERIVED_FROM)
    await session.commit()
    assert len(await store.links(downstream_revision_id=down.id)) == 1


async def test_records_are_idempotent_on_their_keys(session) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    run = await store.insert_agent_run(
        idempotency_key="run-1",
        role=AgentRole.WRITER_DIRECTOR,
        step="script",
        snapshot_object_key="agent-runs/x/context.json",
        snapshot_sha256="c" * 64,
        outcome=AgentRunOutcome.SUCCEEDED,
        episode_id=episode_id,
    )
    again = await store.insert_agent_run(
        idempotency_key="run-1",
        role=AgentRole.WRITER_DIRECTOR,
        step="script",
        snapshot_object_key="other",
        snapshot_sha256="d" * 64,
        outcome=AgentRunOutcome.FAILED,
        episode_id=episode_id,
    )
    assert again.agent_run_id == run.agent_run_id and again.snapshot_sha256 == "c" * 64
    note = await store.insert_annotation(
        episode_id=episode_id,
        kind=AnnotationKind.COMMENT,
        anchor={"segment_id": "s1"},
        body="hello",
        actor=HUMAN,
        command_id="cmd-1",
    )
    note_again = await store.insert_annotation(
        episode_id=episode_id,
        kind=AnnotationKind.COMMENT,
        anchor={"segment_id": "s1"},
        body="hello",
        actor=HUMAN,
        command_id="cmd-1",
    )
    assert note.annotation_id == note_again.annotation_id
    cs = await store.insert_change_set(
        episode_id=episode_id,
        kind=ChangeKind.SCRIPT_EDIT_SCENES,
        base_revision_id=run.agent_run_id,
        proposal={},
        actor=HUMAN,
        proposed_by_command_id="cmd-2",
    )
    cs_again = await store.insert_change_set(
        episode_id=episode_id,
        kind=ChangeKind.SCRIPT_EDIT_SCENES,
        base_revision_id=run.agent_run_id,
        proposal={},
        actor=HUMAN,
        proposed_by_command_id="cmd-2",
    )
    assert cs.change_set_id == cs_again.change_set_id


def test_append_only_tables_have_no_update_or_delete_functions() -> None:
    """INV-41 の構造: 追記型の表の内容を書き換える関数をストアに置かない。

    ``episode_events`` の更新は投影の記帳（``projected_at``）だけを許す。
    """
    source = inspect.getsource(store_module)
    for row in (
        "DecisionRecordRow",
        "AnnotationRow",
        "ArtifactManifestRow",
        "RevisionLinkRow",
        "AgentRunRow",
    ):
        assert f"update({row}" not in source
    assert "delete(" not in source
    event_updates = source.split("update(EpisodeEventRow)")[1:]
    assert len(event_updates) == 1
    values = event_updates[0].split(".values(", 1)[1].split(")", 1)[0]
    assert values.replace(" ", "") == "projected_at=_now("


async def test_ensure_manifest_snapshots_current_artifacts_without_bumping_revision(
    session,
) -> None:
    from contracts.states import ArtifactType
    from infrastructure.intervention.manifests import ensure_manifest

    episode_id = await _episode(session)
    artifacts = ArtifactMetadataRepository(session)
    script = await artifacts.record(
        episode_id=episode_id,
        artifact_type=ArtifactType.SCRIPT,
        schema_version="1.0",
        bucket="b",
        object_key="k1",
        sha256="1" * 64,
        size_bytes=1,
    )
    store = InterventionStore(session)
    first = await ensure_manifest(session, store, episode_id, HUMAN)
    again = await ensure_manifest(session, store, episode_id, HUMAN)
    await session.commit()
    assert first.members == {"script": script.id}
    assert again.manifest_id == first.manifest_id
    control = await store.get_control(episode_id)
    assert control.active_manifest_id == first.manifest_id
    assert control.control_revision == 0
