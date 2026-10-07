"""Episode の読み取りモデル（JP-MAS-001 §8/§12、ADR-0041、read_model.py）と INV-45。

SQLite + InMemoryArtifactStore。読み取りは何も書かないことも検査する。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from contracts.artifacts import SCRIPT_ARTIFACT_SCHEMA_VERSION, build_script_artifact
from contracts.intervention import (
    ActorType,
    AnnotationKind,
    ChangeKind,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionMode,
    RevisionLinkKind,
    Scope,
)
from contracts.states import ArtifactType, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import (
    EpisodeControlRow,
    ResearchCallRow,
    ResearchRequestRow,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.manifests import ensure_manifest
from infrastructure.intervention.read_model import (
    LLM_COST_UNMEASURED,
    EpisodeReadModel,
    approval_is_current,
)
from infrastructure.intervention.store import (
    SYSTEM_ACTOR,
    Actor,
    ControlChange,
    InterventionStore,
    default_control,
)
from tests.support.storyboard import BUCKET, SCRIPT_SCENES, create_episode_at_script_ready

HUMAN = Actor(ActorType.HUMAN, "owner")


async def _load(session_factory, store, episode_id: str):
    view = await EpisodeReadModel(session_factory, store).load(episode_id)
    assert view is not None
    return view


async def _put(session_factory, store, episode_id, artifact_type, payload, *, scene_id=None):
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await store.put_json(
        f"episodes/{episode_id}/{artifact_type.value}/{digest}.json", payload
    )
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=artifact_type,
            schema_version="1.0",
            bucket=BUCKET,
            object_key=put.key,
            sha256=put.sha256,
            input_hash=sha256_hex(digest.encode()),
            scene_id=scene_id,
        )
        await session.commit()
    return meta


def _script(episode_id: str, *, narration_s1: str, title: str = "縄文の火") -> dict:
    scenes = [dict(s) for s in SCRIPT_SCENES]
    scenes[0]["narration"] = narration_s1
    return build_script_artifact(
        episode_id=episode_id,
        language="ja",
        title=title,
        hook="この土器、なぜ焦げているのか",
        scenes=scenes,
        metadata={"topic": "縄文土器", "generator": "fake", "generator_model": "fake-model"},
    )


def _storyboard(script_meta, *, description: str = "土器") -> dict:
    return {
        "type": "storyboard",
        "schema_version": "1.0",
        "source_script": {
            "artifact_id": script_meta.id,
            "sha256": script_meta.sha256,
            "schema_version": SCRIPT_ARTIFACT_SCHEMA_VERSION,
        },
        "scenes": [
            {"scene_id": "sb1", "script_scene_id": "s1", "visual_description": description},
            {"scene_id": "sb2", "script_scene_id": "s2", "visual_description": "炉"},
        ],
    }


async def _setup(session_factory, store):
    """script S1 → storyboard SB(S1) の Episode。"""
    episode_id = await create_episode_at_script_ready(session_factory, store, with_script=False)
    s1 = await _put(
        session_factory,
        store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="a"),
    )
    sb = await _put(session_factory, store, episode_id, ArtifactType.STORYBOARD, _storyboard(s1))
    return episode_id, s1, sb


async def _new_method(session_factory, episode_id: str) -> str:
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.lock_control(episode_id)
        await store.update_control(
            episode_id, ControlChange(mode=InterventionMode.CO_PRODUCTION, bump_revision=False)
        )
        manifest = await ensure_manifest(session, store, episode_id, SYSTEM_ACTOR)
        await session.commit()
        return manifest.manifest_id


async def test_a_legacy_episode_reads_with_defaults_and_nothing_is_written(
    session_factory, artifact_store
) -> None:
    episode_id, s1, sb = await _setup(session_factory, artifact_store)
    view = await _load(session_factory, artifact_store, episode_id)

    assert view.phase == "script_ready"
    assert view.control.persisted is False
    assert view.control.mode == "legacy" and view.control.control_state == "running"
    assert view.manifest.persisted is False and view.manifest.manifest_id is None
    members = {m.slot: m for m in view.manifest.members}
    assert members["script"].revision_id == s1.id
    assert members["script"].sha256 == s1.sha256
    assert members["script"].object_key == s1.object_key
    assert members["storyboard"].stale is False
    # 古い Episode の依存は JSON 参照から導出（書かない）
    assert [(e.upstream_revision_id, e.downstream_revision_id) for e in view.derived_links] == [
        (s1.id, sb.id)
    ]
    json.loads(view.model_dump_json())  # JSON にできる
    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(EpisodeControlRow)) == 0


async def test_a_member_built_from_a_non_adopted_upstream_is_stale_unless_rebound(
    session_factory, artifact_store
) -> None:
    episode_id, s1, sb = await _setup(session_factory, artifact_store)
    s2 = await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="b"),
    )
    view = await _load(session_factory, artifact_store, episode_id)
    storyboard = next(m for m in view.manifest.members if m.slot == "storyboard")
    assert storyboard.stale is True
    assert s1.id in storyboard.stale_reasons[0] and s2.id in storyboard.stale_reasons[0]

    # 構成の同じ新台本へ結び直した storyboard は stale にしない（ADR-0041 §6）
    async with session_factory() as session:
        await InterventionStore(session).add_link(
            episode_id, s2.id, sb.id, RevisionLinkKind.REBOUND_TO
        )
        await session.commit()
    view = await _load(session_factory, artifact_store, episode_id)
    storyboard = next(m for m in view.manifest.members if m.slot == "storyboard")
    assert storyboard.stale is False


def _annotation_stub(kind: AnnotationKind, manifest_id: str | None):
    from infrastructure.intervention.store import Annotation

    return Annotation(
        annotation_id=str(uuid.uuid4()),
        episode_id=str(uuid.uuid4()),
        kind=kind,
        anchor={},
        body="ok",
        payload=None,
        refers_to_annotation_id=None,
        manifest_id=manifest_id,
        actor=HUMAN,
        command_id=None,
        agent_run_id=None,
        created_at=datetime.now(UTC),
    )


def test_approval_is_current_only_for_the_active_manifest() -> None:
    """INV-45 の述語。manifest が変われば古い approval は流用されない。"""
    m1, m2 = str(uuid.uuid4()), str(uuid.uuid4())
    control = default_control(str(uuid.uuid4()))
    on_m1 = _annotation_stub(AnnotationKind.APPROVAL, m1)
    from dataclasses import replace

    assert approval_is_current(on_m1, replace(control, active_manifest_id=m1)) is True
    assert approval_is_current(on_m1, replace(control, active_manifest_id=m2)) is False
    assert approval_is_current(on_m1, control) is False  # 採用 manifest が無い
    assert approval_is_current(_annotation_stub(AnnotationKind.APPROVAL, None), control) is False
    comment = _annotation_stub(AnnotationKind.COMMENT, m1)
    assert approval_is_current(comment, replace(control, active_manifest_id=m1)) is False


async def test_an_approval_of_the_old_manifest_is_stale_after_the_manifest_changes(
    session_factory, artifact_store
) -> None:
    """INV-45: 台本が変わって manifest が変われば、旧 manifest の合格は現行の合格にならない。"""
    episode_id, _s1, _sb = await _setup(session_factory, artifact_store)
    m1 = await _new_method(session_factory, episode_id)
    async with session_factory() as session:
        await InterventionStore(session).insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.APPROVAL,
            anchor={"manifest_id": m1},
            body="before_upload ok",
            actor=HUMAN,
            manifest_id=m1,
        )
        await session.commit()
    view = await _load(session_factory, artifact_store, episode_id)
    (approval,) = [a for a in view.annotations if a.kind == "approval"]
    assert approval.approval_current is True and view.stale_approvals == []

    await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="b"),
    )
    m2 = await _new_method(session_factory, episode_id)
    assert m2 != m1
    view = await _load(session_factory, artifact_store, episode_id)
    (approval,) = [a for a in view.annotations if a.kind == "approval"]
    assert view.manifest.manifest_id == m2
    assert approval.approval_current is False
    assert view.stale_approvals == [approval.annotation_id]


async def test_a_comment_on_a_replaced_revision_stays_put_as_an_old_version_comment(
    session_factory, artifact_store
) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    async with session_factory() as session:
        store = InterventionStore(session)
        old = await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": s1.id, "segment_id": "s1"},
            body="言い回しが硬い",
            actor=HUMAN,
        )
        done = await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.COMMENT,
            anchor={"revision_id": s1.id, "segment_id": "s2"},
            body="ok?",
            actor=HUMAN,
        )
        await store.insert_annotation(
            episode_id=episode_id,
            kind=AnnotationKind.RESOLUTION,
            anchor={},
            body="resolved",
            actor=HUMAN,
            refers_to_annotation_id=done.annotation_id,
        )
        await session.commit()
    await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="b"),
    )
    view = await _load(session_factory, artifact_store, episode_id)
    by_id = {a.annotation_id: a for a in view.annotations}
    assert by_id[old.annotation_id].old_version is True
    assert by_id[old.annotation_id].anchor == {"revision_id": s1.id, "segment_id": "s1"}
    assert by_id[old.annotation_id].status == "open"
    assert by_id[done.annotation_id].status == "resolved"


async def test_received_commands_are_never_presented_as_done(
    session_factory, artifact_store
) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    async with session_factory() as session:
        store = InterventionStore(session)
        for command_id in ("cmd-received-1", "cmd-applied-1"):
            await store.insert_command(
                command_id=command_id,
                episode_id=episode_id,
                type=CommandType.COMMENT,
                actor=HUMAN,
                granted_scope=Scope.COMMENT,
                expected_control_revision=None,
                expected_target_revision=None,
                payload={"x": command_id},
                payload_sha256=command_id,
                correlation_id=None,
            )
        await store.set_command_status("cmd-applied-1", CommandStatus.APPLIED, result={"ok": 1})
        await store.insert_change_set(
            episode_id=episode_id,
            kind=ChangeKind.SCRIPT_EDIT_SCENES,
            base_revision_id=s1.id,
            proposal={"edits": []},
            actor=Actor(ActorType.AI_ASSISTANT, "assistant"),
            proposed_by_command_id="cmd-received-1",
            impact={"regenerate": ["scene_voice"]},
            estimated_cost_usd=Decimal("0.0000"),
        )
        await session.commit()
    view = await _load(session_factory, artifact_store, episode_id)
    commands = {c.command_id: c for c in view.commands}
    assert commands["cmd-received-1"].done is False
    assert "not applied" in commands["cmd-received-1"].display_status
    assert commands["cmd-applied-1"].done is True
    (change_set,) = view.change_sets
    assert change_set.impact == {"regenerate": ["scene_voice"]}
    assert change_set.estimated_cost_usd == "0.0000"
    assert change_set.actor_type == "ai_assistant"


async def test_reservations_are_classified_and_llm_cost_is_unmeasured_not_zero(
    session_factory, artifact_store
) -> None:
    episode_id, _s1, _sb = await _setup(session_factory, artifact_store)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        in_flight = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="k-video",
            input_hash="h1",
            round=1,
            scene_id="sb1",
            estimated_cost_usd=Decimal("0.5000"),
        )
        unknown = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key="k-image",
            input_hash="h2",
            round=1,
            scene_id="sb2",
            estimated_cost_usd=Decimal("0.0300"),
        )
        undispatched = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key="k-image-2",
            input_hash="h3",
            round=1,
            scene_id="sb1",
            estimated_cost_usd=Decimal("0.0300"),
        )
        codex = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.CODEX_SCRIPT,
            idempotency_key="k-codex",
            input_hash="h4",
            round=1,
        )
        await session.commit()
        await repo.mark_dispatched(in_flight.id)
        await repo.record_provider_job_ref(in_flight.id, "job-1")
        await repo.mark_dispatched(unknown.id)
        await repo.mark_dispatched(codex.id)
        await repo.mark_spent(codex.id, raw_output_key="provider-raw/x.txt")
        request_id = uuid.uuid4()
        now = datetime.now(UTC)
        session.add(
            ResearchRequestRow(
                id=request_id,
                idempotency_key="rk",
                request_hash="0" * 64,
                kind="evidence",
                status="completed",
                requester="test",
                channel_id="c",
                episode_id=uuid.UUID(episode_id),
                payload={},
                limits={},
                policy_version="p",
                schema_version="1.0",
                provider_config_version="v",
                as_of=now,
            )
        )
        session.add(
            ResearchCallRow(
                request_id=request_id,
                provider_call="search",
                call_seq=1,
                idempotency_key="rc",
                input_hash="0" * 64,
                provider="fake",
                status="spent",
                estimated_cost_usd=Decimal("0.0200"),
                settled_at=now,
            )
        )
        await session.commit()

    view = await _load(session_factory, artifact_store, episode_id)
    assert [r.reservation_id for r in view.reservations.in_flight] == [in_flight.id]
    assert [r.reservation_id for r in view.reservations.unknown_outcome] == [unknown.id]
    assert [r.reservation_id for r in view.reservations.undispatched] == [undispatched.id]
    costs = view.costs
    assert costs.llm == LLM_COST_UNMEASURED
    assert costs.provider_usd == "0.5600"
    assert costs.research_usd == "0.0200"
    assert costs.measured_usd == "0.5800"
    assert [(u.provider, u.calls) for u in costs.unmeasured] == [("codex_script", 1)]


async def test_records_events_and_derived_topic_decisions_are_listed(
    session_factory, artifact_store
) -> None:
    episode_id, _s1, _sb = await _setup(session_factory, artifact_store)
    await _new_method(session_factory, episode_id)
    async with session_factory() as session:
        store = InterventionStore(session)
        await store.lock_control(episode_id)
        await store.update_control(
            episode_id, ControlChange(control_state=ControlState.HOLD_REQUESTED)
        )
        from contracts.intervention import InterventionEvent

        await store.append_event(episode_id, InterventionEvent.HOLD_REQUESTED, HUMAN, {"a": 1})
        await session.commit()
    view = await _load(session_factory, artifact_store, episode_id)
    assert view.control.persisted is True
    assert view.control.control_state == "hold_requested"
    assert view.control.mode == "co_production"
    assert [e.event_name for e in view.events] == ["intervention.hold.requested"]
    assert view.manifest.persisted is True


async def test_unknown_episode_is_none(session_factory, artifact_store) -> None:
    assert await EpisodeReadModel(session_factory, artifact_store).load(str(uuid.uuid4())) is None


# ------------------------------------------------------------------ 版の比較


async def test_compare_scripts_per_segment(session_factory, artifact_store) -> None:
    episode_id, s1, _sb = await _setup(session_factory, artifact_store)
    s2 = await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        _script(episode_id, narration_s1="b", title="新しい題"),
    )
    diff = await EpisodeReadModel(session_factory, artifact_store).compare_revisions(s1.id, s2.id)
    assert diff.kind == "script"
    assert [(c.path, c.before, c.after) for c in diff.changes] == [
        ("title", "縄文の火", "新しい題"),
        ("segments[s1].narration", "a", "b"),
    ]


async def test_compare_storyboards_per_scene_and_others_generically(
    session_factory, artifact_store
) -> None:
    episode_id, s1, sb = await _setup(session_factory, artifact_store)
    sb2 = await _put(
        session_factory,
        artifact_store,
        episode_id,
        ArtifactType.STORYBOARD,
        _storyboard(s1, description="浮世絵"),
    )
    model = EpisodeReadModel(session_factory, artifact_store)
    diff = await model.compare_revisions(sb.id, sb2.id)
    assert diff.kind == "storyboard"
    assert [(c.path, c.before, c.after) for c in diff.changes] == [
        ("scenes[sb1].visual_description", "土器", "浮世絵")
    ]
    a = await _put(
        session_factory, artifact_store, episode_id, ArtifactType.PRODUCTION_MANIFEST, {"x": [1, 2]}
    )
    b = await _put(
        session_factory, artifact_store, episode_id, ArtifactType.PRODUCTION_MANIFEST, {"x": [1, 3]}
    )
    generic = await model.compare_revisions(a.id, b.id)
    assert generic.kind == "generic"
    assert [(c.path, c.before, c.after) for c in generic.changes] == [("x[1]", 2, 3)]
