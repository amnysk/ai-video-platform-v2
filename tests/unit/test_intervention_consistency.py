"""DB とオブジェクトストアの整合検査（``scripts/consistency-check.py``、設計 §14・§17-11）。

SQLite + メモリストア。バックアップ復元後に「判断・版・素材参照が整合する」を機械で言えること、
壊れた復元（オブジェクト欠落・中身の違い・行の欠落）を必ず報告することを見る。
実の PostgreSQL + MinIO での復元演習は ``scripts/intervention-backup-drill.sh``（隔離スタック）。
理由は docs/testing/intervention-d-rationale.md §5。
"""

from __future__ import annotations

import pathlib
import uuid
from dataclasses import dataclass

import pytest

from contracts.intervention import (
    ActorType,
    AgentRole,
    AgentRunOutcome,
    AnnotationKind,
    ChangeKind,
    CommandType,
    InterventionEvent,
    RevisionLinkKind,
    Scope,
)
from domain.artifact.hashing import sha256_hex
from infrastructure.intervention.consistency import check_consistency
from infrastructure.intervention.manifests import ensure_manifest
from infrastructure.intervention.store import Actor, ControlChange, InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.render_activity import RenderSeed, seed_render_inputs

HUMAN = Actor(ActorType.HUMAN, "owner")


@dataclass
class World:
    seed: RenderSeed
    manifest_id: str
    agent_run_id: str
    snapshot_key: str
    command_id: str


async def _world(session_factory, store: InMemoryArtifactStore) -> World:
    """Artifact 一式（媒体つき）と、全ての介入の表に1行以上ある整合した状態。"""
    seed = await seed_render_inputs(session_factory, store)
    snapshot = b'{"inputs": ["script"]}'
    snapshot_key = f"agent-runs/{seed.episode_id}/run-1/context.json"
    await store.put_bytes(snapshot_key, snapshot, "application/json")
    command_id = f"cmd-{seed.episode_id[:12]}"
    async with session_factory() as session:
        store_db = InterventionStore(session)
        manifest = await ensure_manifest(session, store_db, seed.episode_id, HUMAN)
        await store_db.insert_command(
            command_id=command_id,
            episode_id=seed.episode_id,
            type=CommandType.COMMENT,
            actor=HUMAN,
            granted_scope=Scope.COMMENT,
            expected_control_revision=None,
            expected_target_revision=None,
            payload={},
            payload_sha256="0" * 64,
            correlation_id=None,
        )
        run = await store_db.insert_agent_run(
            idempotency_key=f"run-{seed.episode_id}",
            role=AgentRole.WRITER_DIRECTOR,
            step="script",
            snapshot_object_key=snapshot_key,
            snapshot_sha256=sha256_hex(snapshot),
            outcome=AgentRunOutcome.SUCCEEDED,
            episode_id=seed.episode_id,
            output_revision_id=seed.script.id,
        )
        await store_db.insert_decision(
            idempotency_key=f"d-{seed.episode_id}",
            decision_type="script",
            actor=HUMAN,
            payload={},
            episode_id=seed.episode_id,
            agent_run_id=run.agent_run_id,
            result_revision_id=seed.script.id,
            command_id=command_id,
        )
        await store_db.add_link(
            seed.episode_id, seed.script.id, seed.storyboard.id, RevisionLinkKind.DERIVED_FROM
        )
        await store_db.insert_change_set(
            episode_id=seed.episode_id,
            kind=ChangeKind.SCRIPT_EDIT_SCENES,
            base_revision_id=seed.script.id,
            proposal={},
            actor=HUMAN,
            proposed_by_command_id=command_id,
            base_manifest_id=manifest.manifest_id,
        )
        await store_db.insert_annotation(
            episode_id=seed.episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={},
            body="b",
            actor=HUMAN,
            manifest_id=manifest.manifest_id,
            command_id=command_id,
            agent_run_id=run.agent_run_id,
        )
        await store_db.append_event(
            seed.episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {}, command_id=command_id
        )
        await session.commit()
    return World(seed, manifest.manifest_id, run.agent_run_id, snapshot_key, command_id)


async def _kinds(session_factory, store, **kw) -> set[tuple[str, str]]:
    report = await check_consistency(session_factory, store, **kw)
    return {(p.kind, p.table) for p in report.problems}


async def test_a_consistent_state_has_no_problems(session_factory) -> None:
    store = InMemoryArtifactStore()
    await _world(session_factory, store)
    report = await check_consistency(session_factory, store)
    assert report.ok, [p.line() for p in report.problems]
    assert report.checked_objects > 10 and report.checked_refs > 10


async def test_missing_and_changed_objects_are_reported(session_factory) -> None:
    store = InMemoryArtifactStore()
    world = await _world(session_factory, store)
    del store._objects[world.seed.script.object_key]
    store._objects[world.snapshot_key] = b"tampered"
    assert await _kinds(session_factory, store) == {
        ("missing_object", "artifact_metadata"),
        ("sha256_mismatch", "agent_runs"),
    }


async def test_missing_media_inside_an_artifact_is_reported_unless_skipped(
    session_factory,
) -> None:
    store = InMemoryArtifactStore()
    world = await _world(session_factory, store)
    video = await store.get_json(world.seed.videos["sb1"].object_key)
    del store._objects[video["media"]["object_key"]]
    assert await _kinds(session_factory, store) == {("missing_object", "artifact_metadata")}
    assert await _kinds(session_factory, store, check_media=False) == set()


async def test_dangling_intervention_references_are_reported(session_factory) -> None:
    store = InMemoryArtifactStore()
    world = await _world(session_factory, store)
    ghost = str(uuid.uuid4())
    async with session_factory() as session:
        db = InterventionStore(session)
        await db.lock_control(world.seed.episode_id)
        await db.update_control(
            world.seed.episode_id, ControlChange(waiting_manifest_id=ghost, bump_revision=False)
        )
        await db.insert_manifest(
            world.seed.episode_id, {"script": ghost}, actor=HUMAN, parent_manifest_id=ghost
        )
        await db.add_link(
            world.seed.episode_id, ghost, world.seed.storyboard.id, RevisionLinkKind.PARENT
        )
        await session.commit()
    assert await _kinds(session_factory, store) == {
        ("dangling_ref", "episode_controls"),
        ("dangling_ref", "artifact_manifests"),
        ("dangling_ref", "revision_links"),
    }


async def test_a_reference_into_another_episode_is_reported(session_factory) -> None:
    store = InMemoryArtifactStore()
    world = await _world(session_factory, store)
    other = await _world(session_factory, store)
    async with session_factory() as session:
        await InterventionStore(session).insert_manifest(
            world.seed.episode_id,
            {"script": other.seed.script.id},
            actor=HUMAN,
            parent_manifest_id=world.manifest_id,
        )
        await session.commit()
    assert await _kinds(session_factory, store) == {("episode_mismatch", "artifact_manifests")}


# --------------------------------------------------------------------------- scripts/


def _script():
    from tests.support.script_loader import load_script_module

    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "consistency-check.py"
    return load_script_module("consistency_check", path)


async def test_script_exit_codes(session_factory, capsys) -> None:
    store = InMemoryArtifactStore()
    world = await _world(session_factory, store)
    script = _script()
    assert await script.main([], session_factory=session_factory, store=store) == 0
    assert "consistency: OK" in capsys.readouterr().out
    del store._objects[world.seed.storyboard.object_key]
    assert await script.main([], session_factory=session_factory, store=store) == 1
    out = capsys.readouterr().out
    assert "missing_object\tartifact_metadata" in out and "consistency: NG problems=1" in out


def test_script_rejects_unknown_arguments() -> None:
    with pytest.raises(SystemExit) as exc:
        _script().parse_args(["--fix"])
    assert exc.value.code == 2
