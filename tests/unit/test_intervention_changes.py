"""ChangeSet の提案・適用（ADR-0041、``docs/domain/intervention.md`` §7・§9）。

SQLite・InMemoryArtifactStore。有料 provider は呼ばない（見積りは既存の見積り式の値だけ）。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select

from contracts.artifacts import ScriptArtifact, parse_scene_visual_override_artifact
from contracts.intervention import (
    ActorType,
    AnnotationKind,
    ChangeSetStatus,
    CommandStatus,
    CommandType,
    InterventionEvent,
    RejectReason,
    RevisionLinkKind,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import ArtifactMetadataRepository, ProviderReservationRepository
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.gate import missing_required_records
from infrastructure.intervention.manifests import current_members, ensure_manifest
from infrastructure.intervention.store import SYSTEM_ACTOR, InterventionStore
from infrastructure.production.effective_scene_loader import load_current_override
from tests.support.intervention import (
    AI,
    HUMAN,
    envelope,
    make_episode,
    receive,
    seed_script_and_storyboard,
    submit,
)

RATE = BudgetPolicy(usd_jpy_rate=Decimal("150"))


async def _setup(session, artifact_store, status=EpisodeStatus.ASSETS_READY):
    episode_id = await make_episode(session, status)
    script, storyboard, media = await seed_script_and_storyboard(
        session, artifact_store, episode_id
    )
    return episode_id, script, storyboard, media


def _script_edit(base_id: str, **extra) -> dict:
    return {
        "change": {
            "kind": "script.edit_scenes",
            "base_revision_id": base_id,
            "edits": [{"segment_id": "s2", "narration": "煮炊きの跡が残っている。"}],
            **extra,
        },
        "rationale": "語尾を事実に寄せる",
    }


def _override(base_id: str, scene_id: str = "sb2") -> dict:
    return {
        "change": {
            "kind": "scene.override_visual",
            "base_revision_id": base_id,
            "scene_id": scene_id,
            "visual_description": "炉の跡の遺構を真上から",
        }
    }


async def _propose(session_factory, artifact_store, episode_id, payload, principal=HUMAN):
    outcome = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.PROPOSE_CHANGE, payload),
        principal,
    )
    assert outcome.status is CommandStatus.APPLIED, outcome.result
    return outcome


async def _apply_change(
    session_factory, artifact_store, episode_id, change_set_id, principal=HUMAN, policy=None, **kw
):
    return await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.APPLY_CHANGE, {"change_set_id": change_set_id}, **kw),
        principal,
        policy,
    )


async def _row(session_factory, revision_id: str) -> ArtifactMetadataRow:
    async with session_factory() as s:
        row = await s.get(ArtifactMetadataRow, uuid.UUID(revision_id))
    assert row is not None
    return row


# --------------------------------------------------------------------------- script.edit_scenes


async def test_script_edit_creates_a_new_revision_and_keeps_the_old_one_and_comments(
    session, session_factory, artifact_store
) -> None:
    """INV-41: 新 revision・旧版と注記は残る・結び直し・新 manifest・判断記録・承認の無効化。"""
    episode_id, script, storyboard, media = await _setup(session, artifact_store)
    store = InterventionStore(session)
    old_manifest = await ensure_manifest(session, store, episode_id, SYSTEM_ACTOR)
    approval = await store.insert_annotation(
        episode_id=episode_id,
        kind=AnnotationKind.APPROVAL,
        anchor={"manifest_id": old_manifest.manifest_id},
        body="ok",
        actor=HUMAN.actor,
        payload={"point": "before_production"},
        manifest_id=old_manifest.manifest_id,
    )
    await session.commit()
    await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.COMMENT, {"anchor": {"revision_id": script.id}, "body": "s2 が曖昧"}),
    )

    proposed = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    impact = proposed.result["impact"]
    assert impact["estimated_paid_cost_usd"] == "0"
    assert impact["llm_cost"] == "unmeasured"
    assert impact["restart_stage"] == "production"
    assert "scene_image:sb1" in impact["reuse"] and "storyboard" in impact["reuse"]
    applied = await _apply_change(
        session_factory, artifact_store, episode_id, proposed.result["change_set_id"]
    )
    assert applied.status is CommandStatus.APPLIED, applied.result
    new_id = applied.result["revision_id"]

    old_row, new_row = await _row(session_factory, script.id), await _row(session_factory, new_id)
    assert old_row.superseded_at is not None and new_row.superseded_at is None
    assert new_row.version == old_row.version + 1
    assert await artifact_store.exists(old_row.object_key)
    edited = ScriptArtifact.model_validate(await artifact_store.get_json(new_row.object_key))
    assert edited.scenes[1].narration == "煮炊きの跡が残っている。"
    assert [(s.id, s.duration_ms) for s in edited.scenes] == [
        ("s1", 8000),
        ("s2", 9000),
        ("s3", 8000),
    ]
    # 台本工程の再実行が人の修正を上書きしないよう、再利用キーを引き継ぐ
    assert new_row.input_hash == old_row.input_hash

    async with session_factory() as s:
        store = InterventionStore(s)
        links = {
            (link.upstream_revision_id, link.downstream_revision_id, link.kind)
            for link in await store.links(episode_id=episode_id)
        }
        assert (script.id, new_id, RevisionLinkKind.PARENT) in links
        assert (new_id, storyboard.id, RevisionLinkKind.REBOUND_TO) in links
        control = await store.get_control(episode_id)
        manifest = await store.get_manifest(applied.result["manifest_id"])
        assert manifest is not None
        assert control.active_manifest_id == manifest.manifest_id
        assert manifest.parent_manifest_id == old_manifest.manifest_id
        assert manifest.causation_command_id == applied.command_id
        assert manifest.members["script"] == new_id
        assert manifest.members["storyboard"] == storyboard.id
        (decision,) = await store.list_decisions(result_revision_id=new_id)
        assert decision.actor == HUMAN.actor and decision.command_id == applied.command_id
        assert decision.payload["override_diff"]["script"]["scenes"][0]["segment_id"] == "s2"
        notes = await store.list_annotations(episode_id, kind=AnnotationKind.COMMENT)
        assert [n.anchor["revision_id"] for n in notes] == [script.id]
        invalidated = [
            e.refs["annotation_id"]
            for e in await store.list_events(episode_id)
            if e.event_name is InterventionEvent.REVIEW_INVALIDATED
        ]
        assert invalidated == [approval.annotation_id]
        # INV-46: 人の変更を表す判断記録が新しい台本に結び付く
        assert "script" not in await missing_required_records(s, episode_id)


async def test_script_edit_does_not_touch_image_or_video_identity(
    session, session_factory, artifact_store
) -> None:
    """課金の安全: 台本の文面の修正で、画像・動画（有料）の現行・入力指紋・storyboard は変わらない。

    音声・production_manifest・final_video だけが現行から降りる（ローカル TTS・render で作り直す）。
    """
    episode_id, script, storyboard, media = await _setup(session, artifact_store)
    proposed = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    applied = await _apply_change(
        session_factory, artifact_store, episode_id, proposed.result["change_set_id"]
    )
    assert applied.status is CommandStatus.APPLIED
    async with session_factory() as s:
        members = await current_members(s, episode_id)
    for n in range(1, 5):
        for kind in ("scene_image", "scene_video"):
            meta = media[f"{kind}:sb{n}"]
            assert members[f"{kind}:sb{n}"] == meta.id
            row = await _row(session_factory, meta.id)
            assert row.superseded_at is None
            assert row.input_hash == f"{kind}-sb{n}".ljust(64, "0")[:64]
        assert f"scene_voice:sb{n}" not in members
    assert members["storyboard"] == storyboard.id
    assert "production_manifest" not in members
    assert applied.result["estimated_paid_cost_usd"] == "0"
    async with session_factory() as s:
        reservations = await ProviderReservationRepository(s).list_for_episode_provider(
            episode_id, ProviderCall.FAL_IMAGE
        )
    assert reservations == []


async def test_a_stale_base_revision_is_a_conflict_and_marks_the_change_set(
    session, session_factory, artifact_store
) -> None:
    """INV-43: 先に別の変更が採用されたら、古い版に対する案は上書きせず conflict。"""
    episode_id, script, _storyboard, _media = await _setup(session, artifact_store)
    first = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    second_payload = _script_edit(script.id)
    second_payload["change"]["edits"] = [{"segment_id": "s3", "narration": "定住を食が支えた。"}]
    second = await _propose(session_factory, artifact_store, episode_id, second_payload)
    ok = await _apply_change(
        session_factory, artifact_store, episode_id, first.result["change_set_id"]
    )
    assert ok.status is CommandStatus.APPLIED
    late = await _apply_change(
        session_factory, artifact_store, episode_id, second.result["change_set_id"]
    )
    assert late.status is CommandStatus.CONFLICT
    assert late.reject_reason is RejectReason.STALE_TARGET_REVISION
    assert late.result["current"] == ok.result["revision_id"]
    async with session_factory() as s:
        store = InterventionStore(s)
        cs = await store.get_change_set(second.result["change_set_id"])
        assert cs is not None and cs.status is ChangeSetStatus.CONFLICTED
        current = await ArtifactMetadataRepository(s).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
    assert current is not None and current.id == ok.result["revision_id"]


async def test_an_expected_target_revision_mismatch_is_a_conflict(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, _storyboard, _media = await _setup(session, artifact_store)
    proposed = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    outcome = await _apply_change(
        session_factory,
        artifact_store,
        episode_id,
        proposed.result["change_set_id"],
        expected_target_revision=str(uuid.uuid4()),
    )
    assert outcome.status is CommandStatus.CONFLICT
    async with session_factory() as s:
        cs = await InterventionStore(s).get_change_set(proposed.result["change_set_id"])
    assert cs is not None and cs.status is ChangeSetStatus.PROPOSED


async def test_ai_may_apply_only_zero_cost_changes_that_respect_user_constraints(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, storyboard, _media = await _setup(session, artifact_store)
    await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(
            CommandType.SET_USER_CONSTRAINT, {"rule": "keep_segment_text", "target_ids": ["s2"]}
        ),
    )
    touching = await _propose(
        session_factory, artifact_store, episode_id, _script_edit(script.id), AI
    )
    assert touching.result["impact"]["constraint_conflict"] is True
    refused = await _apply_change(
        session_factory, artifact_store, episode_id, touching.result["change_set_id"], AI
    )
    assert refused.reject_reason is RejectReason.USER_CONSTRAINT_VIOLATION

    paid = await _propose(session_factory, artifact_store, episode_id, _override(storyboard.id), AI)
    refused_paid = await _apply_change(
        session_factory, artifact_store, episode_id, paid.result["change_set_id"], AI, RATE
    )
    assert refused_paid.reject_reason is RejectReason.FORBIDDEN

    clean = _script_edit(script.id)
    clean["change"]["edits"] = [{"segment_id": "s1", "narration": "縄文土器に焦げ跡がある。"}]
    allowed = await _propose(session_factory, artifact_store, episode_id, clean, AI)
    applied = await _apply_change(
        session_factory, artifact_store, episode_id, allowed.result["change_set_id"], AI
    )
    assert applied.status is CommandStatus.APPLIED
    async with session_factory() as s:
        (decision,) = await InterventionStore(s).list_decisions(
            result_revision_id=applied.result["revision_id"]
        )
    # AI の提案を人の直接編集として記録しない
    assert decision.actor.type is ActorType.AI_ASSISTANT


async def test_reject_change_closes_a_proposal_once(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, _storyboard, _media = await _setup(session, artifact_store)
    proposed = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    cs_id = proposed.result["change_set_id"]
    payload = {"change_set_id": cs_id, "reason": "no"}
    first = await submit(
        session_factory, artifact_store, episode_id, envelope(CommandType.REJECT_CHANGE, payload)
    )
    assert first.status is CommandStatus.APPLIED
    again = await submit(
        session_factory, artifact_store, episode_id, envelope(CommandType.REJECT_CHANGE, payload)
    )
    assert again.reject_reason is RejectReason.INVALID_STATE
    applying = await _apply_change(session_factory, artifact_store, episode_id, cs_id)
    assert applying.reject_reason is RejectReason.INVALID_STATE


async def test_changes_are_refused_while_a_stage_runs_or_after_upload(
    session, session_factory, artifact_store
) -> None:
    for status in (EpisodeStatus.IN_PROGRESS, EpisodeStatus.UPLOADED):
        episode_id, script, _storyboard, _media = await _setup(session, artifact_store, status)
        proposed = await _propose(
            session_factory, artifact_store, episode_id, _script_edit(script.id)
        )
        outcome = await _apply_change(
            session_factory, artifact_store, episode_id, proposed.result["change_set_id"]
        )
        assert outcome.reject_reason is RejectReason.INVALID_STATE, status


# --------------------------------------------------------------------------- scene.override_visual


async def test_a_paid_change_without_a_rate_is_unverifiable_and_with_a_rate_is_checked(
    session, session_factory, artifact_store
) -> None:
    episode_id, _script, storyboard, _media = await _setup(session, artifact_store)
    proposed = await _propose(session_factory, artifact_store, episode_id, _override(storyboard.id))
    cost = Decimal(proposed.result["impact"]["estimated_paid_cost_usd"])
    assert cost > 0  # 画像1枚 + 5秒の動画（既存の見積り式）
    assert proposed.result["budget_check"] == RejectReason.BUDGET_UNVERIFIABLE.value
    cs_id = proposed.result["change_set_id"]
    unverifiable = await _apply_change(session_factory, artifact_store, episode_id, cs_id)
    assert unverifiable.reject_reason is RejectReason.BUDGET_UNVERIFIABLE
    tiny = BudgetPolicy(usd_jpy_rate=Decimal("150"), episode_budget_jpy=1)
    exceeded = await _apply_change(session_factory, artifact_store, episode_id, cs_id, policy=tiny)
    assert exceeded.reject_reason is RejectReason.BUDGET_EXCEEDED
    applied = await _apply_change(session_factory, artifact_store, episode_id, cs_id, policy=RATE)
    assert applied.status is CommandStatus.APPLIED, applied.result


async def test_scene_override_reuses_the_override_artifact_and_regenerates_only_that_scene(
    session, session_factory, artifact_store
) -> None:
    episode_id, _script, storyboard, media = await _setup(session, artifact_store)
    proposed = await _propose(session_factory, artifact_store, episode_id, _override(storyboard.id))
    applied = await _apply_change(
        session_factory, artifact_store, episode_id, proposed.result["change_set_id"], policy=RATE
    )
    assert applied.status is CommandStatus.APPLIED, applied.result
    async with session_factory() as s:
        members = await current_members(s, episode_id)
        storyboard_meta = await ArtifactMetadataRepository(s).get(storyboard.id)
    assert storyboard_meta is not None
    # ADR-0035 の読み口（画像・動画の Activity が使う）がそのまま差し替えを読む
    override = await load_current_override(
        session_factory,
        artifact_store,
        episode_id=episode_id,
        scene_id="sb2",
        storyboard_meta=storyboard_meta,
    )
    assert override is not None
    assert override.visual_description == "炉の跡の遺構を真上から"
    assert override.change_set_id == proposed.result["change_set_id"]
    assert override.rejection_ids == []
    assert members["scene_visual_override:sb2"] == applied.result["revision_id"]
    assert "scene_image:sb2" not in members and "scene_video:sb2" not in members
    for n in (1, 3, 4):
        assert members[f"scene_image:sb{n}"] == media[f"scene_image:sb{n}"].id
        assert members[f"scene_video:sb{n}"] == media[f"scene_video:sb{n}"].id
    assert members["storyboard"] == storyboard.id


async def test_a_planner_override_without_change_set_keeps_its_json_shape() -> None:
    """契約の追加（change_set_id）は ADR-0035 の既存の案のバイト列を変えない。"""
    payload = {
        "episode_id": "e",
        "type": "scene_visual_override",
        "schema_version": "1.0",
        "source_storyboard": {
            "artifact_id": str(uuid.uuid4()),
            "sha256": "a" * 64,
            "schema_version": "1.0",
        },
        "scene_id": "sb1",
        "revision": 1,
        "visual_kind": "broll",
        "visual_subject": "site",
        "visual_description": "遠景",
        "framing": None,
        "camera_movement": None,
        "rationale": "r",
        "rejection_ids": [str(uuid.uuid4())],
        "planner": {"generator": "g", "generator_model": "m", "generation_profile_id": "p"},
    }
    assert parse_scene_visual_override_artifact(payload).model_dump(mode="json") == payload


async def test_an_old_version_result_cannot_become_the_adopted_member_after_a_change(
    session, session_factory, artifact_store
) -> None:
    """課金の安全: 旧版の入力で送った job が差し替えの後に届いて採用されることはない。

    送信済みの job があるシーンの差し替えは受け付けない（届いた結果は画像の Activity が入力の
    不一致で保存を拒み、課金済みの結果が宙に浮くため）。結果が旧版の入力指紋で保存された後なら
    適用でき、その旧版は行として残る（元の input_hash のまま）が、新しい採用 manifest には入らない。
    """
    episode_id, _script, storyboard, media = await _setup(session, artifact_store)
    repo = ProviderReservationRepository(session)
    old_hash = "o" * 64
    reservation = await repo.reserve(
        episode_id=episode_id,
        provider=ProviderCall.FAL_IMAGE,
        idempotency_key="img-sb2-r2",
        input_hash=old_hash,
        round=2,
        scene_id="sb2",
    )
    await session.commit()
    await repo.mark_dispatched(reservation.id)
    await repo.record_provider_job_ref(reservation.id, "ref")
    await session.commit()

    proposed = await _propose(session_factory, artifact_store, episode_id, _override(storyboard.id))
    cs_id = proposed.result["change_set_id"]
    refused = await _apply_change(session_factory, artifact_store, episode_id, cs_id, policy=RATE)
    assert refused.reject_reason is RejectReason.INVALID_STATE
    assert [p["reservation_id"] for p in refused.result["in_flight"]] == [reservation.id]

    # 旧版の結果が届き、既存の経路（spent → record → attach）で旧版の入力指紋のまま保存される
    await repo.mark_spent(reservation.id, raw_output_key="provider-raw/x", reconciled_by="evidence")
    old_result = await ArtifactMetadataRepository(session).record(
        episode_id=episode_id,
        artifact_type=ArtifactType.SCENE_IMAGE,
        schema_version="1.0",
        bucket="artifacts",
        object_key="artifacts/x/scene_image/sb2/old.json",
        sha256="5" * 64,
        input_hash=old_hash,
        scene_id="sb2",
    )
    await repo.attach_artifact(reservation.id, old_result.id)
    await session.commit()

    applied = await _apply_change(session_factory, artifact_store, episode_id, cs_id, policy=RATE)
    assert applied.status is CommandStatus.APPLIED, applied.result
    row = await _row(session_factory, old_result.id)
    assert row.input_hash == old_hash and row.superseded_at is not None
    async with session_factory() as s:
        manifest = await InterventionStore(s).get_manifest(applied.result["manifest_id"])
        reused = await ArtifactMetadataRepository(s).find_current(
            episode_id=episode_id,
            artifact_type=ArtifactType.SCENE_IMAGE,
            input_hash=old_hash,
            scene_id="sb2",
        )
    assert manifest is not None
    assert old_result.id not in manifest.members.values()
    assert media["scene_image:sb1"].id in manifest.members.values()
    assert reused is None


async def test_proposals_against_a_stale_base_are_conflicts_and_do_not_create_change_sets(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, _storyboard, _media = await _setup(session, artifact_store)
    first = await _propose(session_factory, artifact_store, episode_id, _script_edit(script.id))
    await _apply_change(session_factory, artifact_store, episode_id, first.result["change_set_id"])
    stale = await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.PROPOSE_CHANGE, _script_edit(script.id)),
    )
    assert stale.status is CommandStatus.CONFLICT
    async with session_factory() as s:
        assert len(await InterventionStore(s).list_change_sets(episode_id)) == 1


async def test_received_proposal_is_not_applied_until_the_command_runs(
    session, session_factory, artifact_store
) -> None:
    episode_id, script, _storyboard, _media = await _setup(session, artifact_store)
    received = await receive(
        session_factory,
        episode_id,
        envelope(CommandType.PROPOSE_CHANGE, _script_edit(script.id)),
    )
    assert received.command.status is CommandStatus.RECEIVED
    async with session_factory() as s:
        assert await InterventionStore(s).list_change_sets(episode_id) == []
        rows = (await s.scalars(select(ArtifactMetadataRow.id))).all()
    assert len(rows) > 0
