"""DB とオブジェクトストアの整合検査・復元演習の共通データ（JP-MAS-001 / ADR-0041）。

Artifact 一式（媒体つき）と、全ての判断・介入の表に1行以上ある**整合した**状態を作る。
``tests/unit/test_intervention_consistency.py``（SQLite・メモリストア）と
``scripts/intervention-backup-drill.py``（隔離スタックの PostgreSQL・MinIO）が使う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
from infrastructure.intervention.manifests import ensure_manifest
from infrastructure.intervention.store import Actor, InterventionStore
from tests.support.render_activity import RenderSeed, seed_render_inputs

HUMAN = Actor(ActorType.HUMAN, "owner")


@dataclass
class World:
    seed: RenderSeed
    manifest_id: str
    agent_run_id: str
    snapshot_key: str
    command_id: str


async def seed_world(session_factory, store: Any) -> World:
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
