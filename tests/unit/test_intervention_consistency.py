"""DB とオブジェクトストアの整合検査（``scripts/consistency-check.py``、設計 §14・§17-11）。

SQLite + メモリストア。バックアップ復元後に「判断・版・素材参照が整合する」を機械で言えること、
壊れた復元（オブジェクト欠落・中身の違い・行の欠落）を必ず報告することを見る。
実の PostgreSQL + MinIO での復元演習は ``scripts/intervention-backup-drill.sh``（隔離スタック）。
理由は docs/testing/intervention-d-rationale.md §5。
"""

from __future__ import annotations

import pathlib
import uuid

import pytest

from contracts.intervention import (
    RevisionLinkKind,
)
from infrastructure.intervention.consistency import check_consistency
from infrastructure.intervention.store import ControlChange, InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.intervention_world import HUMAN, seed_world


async def _kinds(session_factory, store, **kw) -> set[tuple[str, str]]:
    report = await check_consistency(session_factory, store, **kw)
    return {(p.kind, p.table) for p in report.problems}


async def test_a_consistent_state_has_no_problems(session_factory) -> None:
    store = InMemoryArtifactStore()
    await seed_world(session_factory, store)
    report = await check_consistency(session_factory, store)
    assert report.ok, [p.line() for p in report.problems]
    assert report.checked_objects > 10 and report.checked_refs > 10


async def test_missing_and_changed_objects_are_reported(session_factory) -> None:
    store = InMemoryArtifactStore()
    world = await seed_world(session_factory, store)
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
    world = await seed_world(session_factory, store)
    video = await store.get_json(world.seed.videos["sb1"].object_key)
    del store._objects[video["media"]["object_key"]]
    assert await _kinds(session_factory, store) == {("missing_object", "artifact_metadata")}
    assert await _kinds(session_factory, store, check_media=False) == set()


async def test_dangling_intervention_references_are_reported(session_factory) -> None:
    store = InMemoryArtifactStore()
    world = await seed_world(session_factory, store)
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
    world = await seed_world(session_factory, store)
    other = await seed_world(session_factory, store)
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
    world = await seed_world(session_factory, store)
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
