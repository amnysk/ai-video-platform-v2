"""ChangeSet の提案・適用・却下（``docs/domain/intervention.md`` §7、設計 §9）。

適用は**新しい revision**を作り、古い revision・コメントを消さない（INV-41）。同じ transaction で:

- 新しい成果物を不変のオブジェクトとして保存し（既存のキー規約）、``artifact_metadata`` に
  新しい世代を記録する（古い行は superseded として残る）
- ``revision_links``（``parent`` / ``rebound_to`` / ``derived_from``）
- 影響範囲の下流（音声・production_manifest・final_video、シーンの画像・動画）を現行から降ろす
  （後継は工程の再実行が作る。行は消さない）
- 新しい採用 manifest（``store.insert_manifest``）と ``episode_controls.active_manifest_id``・制御版
- 判断記録（ChangeSet の作者が actor。適用した主体は command 行に残る）と ``override_diff``
- イベント ``intervention.change.applied`` と、古い manifest に結び付いた approval ごとの
  ``review.invalidated``

検証（何も書かない）を先に、書き込みを後にする。基準版が現行でなければ CONFLICT
（上書きしない / INV-43）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select

from contracts.artifacts import (
    SceneVisualOverrideArtifact,
    ScriptArtifact,
    StoryboardArtifact,
    StoryboardScene,
    parse_scene_visual_override_artifact,
)
from contracts.intervention import (
    ActorType,
    AnnotationKind,
    ApplyChangePayload,
    ChangeKind,
    ChangeSetStatus,
    ControlState,
    DecisionCandidate,
    DecisionRecordPayload,
    InterventionEvent,
    ProposeChangePayload,
    RationaleKind,
    RejectChangePayload,
    RejectReason,
    RevisionLinkKind,
    SceneOverrideVisual,
    ScriptEditScenes,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.production.effective_scene import apply_override
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.budget import budget_view, check_additional_cost
from infrastructure.intervention.commands import (
    CLOSED_EPISODE_STATUSES,
    ApplyContext,
    Decision,
)
from infrastructure.intervention.gate import approval_matches, withdrawn_annotation_ids
from infrastructure.intervention.impact import (
    ConstraintRule,
    Impact,
    scene_override_impact,
    script_edit_impact,
)
from infrastructure.intervention.manifests import current_members, ensure_manifest
from infrastructure.intervention.store import (
    SYSTEM_ACTOR,
    ChangeSet,
    ControlChange,
    InterventionStore,
)

#: 人の介入で作った差し替え案の来歴（``SceneVisualOverrideArtifact.planner``）
INTERVENTION_GENERATOR = "intervention-change-set"
INTERVENTION_PROFILE = "change-set-v1"

#: ChangeSet の kind → 基準 revision の成果物の型
BASE_TYPE: dict[ChangeKind, ArtifactType] = {
    ChangeKind.SCRIPT_EDIT_SCENES: ArtifactType.SCRIPT,
    ChangeKind.SCENE_OVERRIDE_VISUAL: ArtifactType.STORYBOARD,
}


class _Refused(Exception):
    """検証で適用しない（まだ何も書いていない）。"""

    def __init__(self, decision: Decision) -> None:
        super().__init__(decision.result.get("reason"))
        self.decision = decision


def _reject(reason: RejectReason, **detail: Any) -> _Refused:
    return _Refused(Decision.rejected(reason, **detail))


# --------------------------------------------------------------------------- 読み取り


async def _current_row(
    ctx: ApplyContext, artifact_type: ArtifactType, scene_id: str | None = None
) -> ArtifactMetadataRow | None:
    stmt = select(ArtifactMetadataRow).where(
        ArtifactMetadataRow.episode_id == uuid.UUID(ctx.episode_id),
        ArtifactMetadataRow.artifact_type == artifact_type.value,
        ArtifactMetadataRow.superseded_at.is_(None),
        ArtifactMetadataRow.scene_id.is_(None)
        if scene_id is None
        else ArtifactMetadataRow.scene_id == scene_id,
    )
    return (await ctx.session.scalars(stmt)).first()


async def _row(ctx: ApplyContext, revision_id: str) -> ArtifactMetadataRow | None:
    try:
        key = uuid.UUID(revision_id)
    except ValueError:
        return None
    row = await ctx.session.get(ArtifactMetadataRow, key)
    if row is None or str(row.episode_id) != ctx.episode_id:
        return None
    return row


async def _load_json(ctx: ApplyContext, row: ArtifactMetadataRow) -> dict[str, Any]:
    assert ctx.artifact_store is not None, "changes need the artifact store"
    payload = await ctx.artifact_store.get_json(row.object_key)
    if sha256_hex(canonical_json_bytes(payload)) != row.sha256:
        raise _reject(
            RejectReason.INVALID_STATE, detail=f"stored {row.artifact_type} does not match its hash"
        )
    return payload


async def _constraints(store: InterventionStore, episode_id: str) -> list[ConstraintRule]:
    annotations = await store.list_annotations(episode_id)
    withdrawn = {
        a.refers_to_annotation_id
        for a in annotations
        if a.kind is AnnotationKind.RESOLUTION and a.refers_to_annotation_id is not None
    }
    rules: list[ConstraintRule] = []
    for a in annotations:
        if a.kind is not AnnotationKind.USER_CONSTRAINT or a.annotation_id in withdrawn:
            continue
        payload = a.payload or {}
        rules.append(
            ConstraintRule(
                annotation_id=a.annotation_id,
                rule=str(payload.get("rule", "")),
                target_ids=tuple(str(t) for t in payload.get("target_ids", [])),
            )
        )
    return rules


async def _approvals_on(store: InterventionStore, episode_id: str, manifest_id: str) -> list[str]:
    annotations = await store.list_annotations(episode_id)
    withdrawn = withdrawn_annotation_ids(annotations)
    return [
        a.annotation_id
        for a in annotations
        if approval_matches(a, manifest_id, withdrawn=withdrawn)
    ]


# --------------------------------------------------------------------------- 変更の組み立て


@dataclass(frozen=True, slots=True)
class _ScriptEdit:
    payload: dict[str, Any]
    diff: dict[str, Any]
    edited_segment_ids: tuple[str, ...]


def build_edited_script(current: dict[str, Any], change: ScriptEditScenes) -> _ScriptEdit:
    """台本の文面だけを差し替えた新しい ScriptArtifact（pydantic で検証済み）を作る。

    シーンの id・順序・尺は変えない（変わるなら storyboard を作り直す必要があり、初期は適用不可）。
    映像の文（``visual``）の修正は受け付けない（画像は storyboard から作られるので何も変わらない。
    映像を変えるのは ``scene.override_visual``）。
    """
    if any(e.visual is not None for e in change.edits):
        # 画像・動画は storyboard から作られ、台本の visual は読まれない。適用しても映像は変わらない
        raise _reject(
            RejectReason.UNSUPPORTED_CHANGE,
            detail="script visual text does not change any image; use scene.override_visual",
        )
    script = ScriptArtifact.model_validate(current)
    by_id = {scene.id: scene for scene in script.scenes}
    unknown = [e.segment_id for e in change.edits if e.segment_id not in by_id]
    if unknown:
        raise _reject(RejectReason.INVALID_PAYLOAD, detail=f"unknown segment ids: {unknown}")
    edits = {e.segment_id: e for e in change.edits}
    scene_diffs: list[dict[str, Any]] = []
    scenes = []
    for scene in script.scenes:
        edit = edits.get(scene.id)
        if edit is None:
            scenes.append(scene.model_dump(mode="json"))
            continue
        after = scene.model_copy(
            update={
                k: v
                for k, v in (("narration", edit.narration), ("visual", edit.visual))
                if v is not None
            }
        )
        scenes.append(after.model_dump(mode="json"))
        if after != scene:
            scene_diffs.append(
                {
                    "segment_id": scene.id,
                    "before": {"narration": scene.narration, "visual": scene.visual},
                    "after": {"narration": after.narration, "visual": after.visual},
                }
            )
    data = script.model_dump(mode="json")
    data["scenes"] = scenes
    if change.title is not None:
        data["title"] = change.title
    if change.hook is not None:
        data["hook"] = change.hook
    try:
        edited = ScriptArtifact.model_validate(data)
    except ValidationError as exc:
        raise _reject(RejectReason.INVALID_PAYLOAD, detail=str(exc)[:1000]) from None
    structure = [(s.id, s.duration_ms) for s in script.scenes]
    if [(s.id, s.duration_ms) for s in edited.scenes] != structure:
        raise _reject(RejectReason.UNSUPPORTED_CHANGE, detail="scene ids, order or durations")
    if edited == script:
        raise _reject(RejectReason.INVALID_PAYLOAD, detail="the edit changes nothing")
    diff: dict[str, Any] = {"scenes": scene_diffs}
    for key in ("title", "hook"):
        if getattr(edited, key) != getattr(script, key):
            diff[key] = {"before": getattr(script, key), "after": getattr(edited, key)}
    return _ScriptEdit(
        payload=edited.model_dump(mode="json"),
        diff=diff,
        edited_segment_ids=tuple(d["segment_id"] for d in scene_diffs),
    )


def _find_scene(storyboard: StoryboardArtifact, scene_id: str) -> StoryboardScene:
    for scene in storyboard.scenes:
        if scene.scene_id == scene_id:
            return scene
    raise _reject(RejectReason.INVALID_PAYLOAD, detail=f"storyboard has no scene {scene_id}")


async def _current_override(
    ctx: ApplyContext, scene_id: str, storyboard_row: ArtifactMetadataRow
) -> tuple[ArtifactMetadataRow | None, SceneVisualOverrideArtifact | None]:
    row = await _current_row(ctx, ArtifactType.SCENE_VISUAL_OVERRIDE, scene_id)
    if row is None:
        return None, None
    override = parse_scene_visual_override_artifact(await _load_json(ctx, row))
    if override.source_storyboard.sha256 != storyboard_row.sha256:
        # 別の storyboard 世代の案は適用されない（effective_scene_loader と同じ規則）
        return row, None
    return row, override


async def _in_flight_for_scene(ctx: ApplyContext, scene_id: str) -> list[dict[str, Any]]:
    """シーンの画像・動画で、まだ結果が保存されていない（されうる）有料予約。

    旧版の入力で送った job の結果が差し替えの**後**に届くと、画像の Activity は入力の不一致で保存を
    拒み（課金済みの結果が宙に浮く）、予約は照合待ちのまま残る。そのシーンの差し替えは、送信済みの
    job が保存（旧 revision として）または失敗で確定するまで受け付けない。
    """
    repo = ProviderReservationRepository(ctx.session)
    pending: list[dict[str, Any]] = []
    for provider in (ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO):
        for r in await repo.list_for_episode_provider(ctx.episode_id, provider):
            if r.scene_id != scene_id:
                continue
            dispatched = r.status is ReservationStatus.RESERVED and r.dispatched_at is not None
            recording = (
                r.status is ReservationStatus.SPENT
                and r.outcome_artifact_id is None
                and r.failure_class is None
                and not r.input_rejected_by_provider
            )
            if dispatched or recording:
                pending.append(
                    {"reservation_id": r.id, "provider": r.provider.value, "status": r.status.value}
                )
    return pending


async def _ledger_estimates(ctx: ApplyContext, scene_id: str) -> tuple[Decimal | None, ...]:
    """シーンの画像・動画を前回送ったときに adapter が台帳へ書いた見積り（最新の予約）。"""
    repo = ProviderReservationRepository(ctx.session)
    found: list[Decimal | None] = []
    for provider in (ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO):
        rows = [
            r
            for r in await repo.list_for_episode_provider(ctx.episode_id, provider)
            if r.scene_id == scene_id and r.estimated_cost_usd is not None
        ]
        found.append(rows[-1].estimated_cost_usd if rows else None)
    return tuple(found)


# --------------------------------------------------------------------------- 提案


async def _impact_for(
    ctx: ApplyContext, change: ScriptEditScenes | SceneOverrideVisual, *, manifest_id: str | None
) -> tuple[Impact, dict[str, Any]]:
    """影響範囲と、適用に使う下ごしらえ（検証済み）。"""
    members = await current_members(ctx.session, ctx.episode_id)
    constraints = await _constraints(ctx.store, ctx.episode_id)
    approvals = (
        [] if manifest_id is None else await _approvals_on(ctx.store, ctx.episode_id, manifest_id)
    )
    if isinstance(change, ScriptEditScenes):
        script_row = await _current_row(ctx, ArtifactType.SCRIPT)
        assert script_row is not None
        edit = build_edited_script(await _load_json(ctx, script_row), change)
        impact = script_edit_impact(
            members=members,
            edited_segment_ids=edit.edited_segment_ids,
            constraints=constraints,
            approvals=approvals,
        )
        return impact, {"edit": edit, "base_row": script_row}
    storyboard_row = await _current_row(ctx, ArtifactType.STORYBOARD)
    assert storyboard_row is not None
    storyboard = StoryboardArtifact.model_validate(await _load_json(ctx, storyboard_row))
    scene = _find_scene(storyboard, change.scene_id)
    image_usd, video_usd = await _ledger_estimates(ctx, change.scene_id)
    impact = scene_override_impact(
        members=members,
        scene_id=change.scene_id,
        image_usd=image_usd,
        video_usd=video_usd,
        constraints=constraints,
        approvals=approvals,
    )
    return impact, {"scene": scene, "base_row": storyboard_row}


async def _check_base_is_current(ctx: ApplyContext, kind: ChangeKind, base_revision_id: str):
    base = await _row(ctx, base_revision_id)
    if base is None or base.artifact_type != BASE_TYPE[kind].value:
        raise _reject(RejectReason.NOT_FOUND, detail="base revision not found for this episode")
    current = await _current_row(ctx, BASE_TYPE[kind])
    if current is None or current.id != base.id:
        return current
    return None


async def propose_change(ctx: ApplyContext) -> Decision:
    payload = ProposeChangePayload.model_validate(ctx.command.payload)
    change = payload.change
    try:
        stale = await _check_base_is_current(ctx, change.kind, change.base_revision_id)
        if stale is not None:
            return Decision.conflict(
                RejectReason.STALE_TARGET_REVISION,
                expected=change.base_revision_id,
                current=str(stale.id),
            )
        impact, _ = await _impact_for(ctx, change, manifest_id=ctx.control.active_manifest_id)
    except _Refused as refused:
        return refused.decision
    view = await budget_view(ctx.session, ctx.episode_id, ctx.budget_policy, ctx.now)
    change_set = await ctx.store.insert_change_set(
        episode_id=ctx.episode_id,
        kind=change.kind,
        base_revision_id=change.base_revision_id,
        proposal=payload.model_dump(mode="json"),
        actor=ctx.actor,
        proposed_by_command_id=ctx.command.command_id,
        base_manifest_id=payload.base_manifest_id or ctx.control.active_manifest_id,
        impact=impact.as_dict(),
        estimated_cost_usd=impact.estimated_paid_cost_usd,
        rationale=payload.rationale,
        variant_group=payload.variant_group,
    )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.CHANGE_PROPOSED,
        ctx.actor,
        {
            "change_set_id": change_set.change_set_id,
            "kind": change.kind.value,
            "estimated_paid_cost_usd": impact.as_dict()["estimated_paid_cost_usd"],
            "constraint_conflict": impact.constraint_conflict,
        },
        command_id=ctx.command.command_id,
    )
    return Decision.applied(
        {
            "change_set_id": change_set.change_set_id,
            "impact": impact.as_dict(),
            "budget": view.as_dict(),
            "budget_check": (
                None
                if (r := check_additional_cost(view, impact.estimated_paid_cost_usd)) is None
                else r.value
            ),
        }
    )


async def reject_change(ctx: ApplyContext) -> Decision:
    payload = RejectChangePayload.model_validate(ctx.command.payload)
    change_set = await _change_set(ctx, payload.change_set_id)
    if change_set is None:
        return Decision.rejected(RejectReason.NOT_FOUND)
    if change_set.status is not ChangeSetStatus.PROPOSED:
        return Decision.rejected(RejectReason.INVALID_STATE, change_set_status=change_set.status)
    await ctx.store.set_change_set_status(
        change_set.change_set_id,
        ChangeSetStatus.REJECTED,
        decided_by_command_id=ctx.command.command_id,
    )
    return Decision.applied({"change_set_id": change_set.change_set_id, "reason": payload.reason})


async def _change_set(ctx: ApplyContext, change_set_id: str) -> ChangeSet | None:
    try:
        change_set = await ctx.store.get_change_set(change_set_id, lock=True)
    except ValueError:
        return None
    if change_set is None or change_set.episode_id != ctx.episode_id:
        return None
    return change_set


# --------------------------------------------------------------------------- 適用


async def apply_change(ctx: ApplyContext) -> Decision:
    payload = ApplyChangePayload.model_validate(ctx.command.payload)
    change_set = await _change_set(ctx, payload.change_set_id)
    if change_set is None:
        return Decision.rejected(RejectReason.NOT_FOUND)
    if change_set.status is not ChangeSetStatus.PROPOSED:
        return Decision.rejected(RejectReason.INVALID_STATE, change_set_status=change_set.status)
    proposal = ProposeChangePayload.model_validate(change_set.proposal)
    change = proposal.change
    try:
        await _check_applicable_state(ctx)
        stale = await _check_base_is_current(ctx, change.kind, change.base_revision_id)
        if stale is not None:
            await ctx.store.set_change_set_status(
                change_set.change_set_id,
                ChangeSetStatus.CONFLICTED,
                decided_by_command_id=ctx.command.command_id,
            )
            await ctx.store.append_event(
                ctx.episode_id,
                InterventionEvent.CHANGE_CONFLICTED,
                ctx.actor,
                {
                    "change_set_id": change_set.change_set_id,
                    "base_revision_id": change.base_revision_id,
                    "current_revision_id": str(stale.id),
                },
                command_id=ctx.command.command_id,
            )
            return Decision.conflict(
                RejectReason.STALE_TARGET_REVISION,
                change_set_id=change_set.change_set_id,
                expected=change.base_revision_id,
                current=str(stale.id),
            )
        expected = ctx.command.expected_target_revision
        if expected is not None and expected != change.base_revision_id:
            return Decision.conflict(
                RejectReason.STALE_TARGET_REVISION,
                expected=expected,
                current=change.base_revision_id,
            )
        base_manifest = await ensure_manifest(ctx.session, ctx.store, ctx.episode_id, SYSTEM_ACTOR)
        impact, prepared = await _impact_for(ctx, change, manifest_id=base_manifest.manifest_id)
        await _check_policy(ctx, impact, payload)
        if isinstance(change, SceneOverrideVisual):
            pending = await _in_flight_for_scene(ctx, change.scene_id)
            if pending:
                raise _reject(
                    RejectReason.INVALID_STATE,
                    detail="paid jobs for this scene are still in flight; wait until they settle",
                    in_flight=pending,
                )
            prepared["override"] = await _prepare_override(
                ctx, change, change_set, prepared["scene"], prepared["base_row"]
            )
    except _Refused as refused:
        return refused.decision

    # ---- ここから書き込み（同じ transaction）
    if isinstance(change, ScriptEditScenes):
        new_meta_id, diff = await _write_script(ctx, prepared)
    else:
        new_meta_id, diff = await _write_override(ctx, change, prepared)
    for slot in impact.regenerate:
        artifact_type, _, scene_id = slot.partition(":")
        await ArtifactMetadataRepository(ctx.session).retire_current(
            ctx.episode_id, ArtifactType(artifact_type), scene_id or None
        )
    await _record_decision(ctx, change_set, change, new_meta_id, diff)
    members = await current_members(ctx.session, ctx.episode_id)
    manifest = await ctx.store.insert_manifest(
        ctx.episode_id,
        members,
        actor=ctx.actor,
        parent_manifest_id=base_manifest.manifest_id,
        causation_command_id=ctx.command.command_id,
    )
    await ctx.store.update_control(
        ctx.episode_id, ControlChange(active_manifest_id=manifest.manifest_id)
    )
    await ctx.store.set_change_set_status(
        change_set.change_set_id,
        ChangeSetStatus.APPLIED,
        decided_by_command_id=ctx.command.command_id,
        applied_revision_id=new_meta_id,
        applied_manifest_id=manifest.manifest_id,
        impact=impact.as_dict(),
    )
    await _close_variants(ctx, change_set)
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.CHANGE_APPLIED,
        ctx.actor,
        {
            "change_set_id": change_set.change_set_id,
            "kind": change.kind.value,
            "revision_id": new_meta_id,
            "manifest_id": manifest.manifest_id,
            "parent_manifest_id": base_manifest.manifest_id,
        },
        command_id=ctx.command.command_id,
    )
    for approval_id in impact.rereview:
        await ctx.store.append_event(
            ctx.episode_id,
            InterventionEvent.REVIEW_INVALIDATED,
            ctx.actor,
            {
                "annotation_id": approval_id,
                "manifest_id": base_manifest.manifest_id,
                "superseded_by_manifest_id": manifest.manifest_id,
            },
            command_id=ctx.command.command_id,
        )
    return Decision.applied(
        {
            "change_set_id": change_set.change_set_id,
            "revision_id": new_meta_id,
            "manifest_id": manifest.manifest_id,
            "parent_manifest_id": base_manifest.manifest_id,
            "stale": list(impact.regenerate),
            "rereview": list(impact.rereview),
            "restart_stage": impact.restart_stage.value,
            "estimated_paid_cost_usd": impact.as_dict()["estimated_paid_cost_usd"],
            "note": (
                "applied as a new revision; regenerating the stale outputs needs a resume "
                "(the final video is not regenerated yet)"
            ),
        }
    )


async def _check_applicable_state(ctx: ApplyContext) -> None:
    """工程が走っていない間だけ適用する（走っている工程は旧版で下流を作り直してしまう）。"""
    if ctx.control.control_state is ControlState.CANCEL_REQUESTED:
        raise _reject(RejectReason.INVALID_STATE, state=ctx.control.control_state.value)
    episode = await EpisodeRepository(ctx.session).get(ctx.episode_id)
    if episode is None:
        raise _reject(RejectReason.NOT_FOUND)
    if episode.status in CLOSED_EPISODE_STATUSES:
        # 投稿済みの動画を変更・再投稿しない（AGENTS.md §9）
        raise _reject(RejectReason.INVALID_STATE, episode_status=episode.status.value)
    if episode.status is EpisodeStatus.IN_PROGRESS:
        raise _reject(
            RejectReason.INVALID_STATE,
            episode_status=episode.status.value,
            detail="a stage is running; request a hold and wait until it stops",
        )


async def _check_policy(ctx: ApplyContext, impact: Impact, payload: ApplyChangePayload) -> None:
    human = ctx.actor.type is ActorType.HUMAN
    cost = impact.estimated_paid_cost_usd
    if impact.constraint_conflict and not human:
        raise _reject(
            RejectReason.USER_CONSTRAINT_VIOLATION,
            violations=list(impact.constraint_violations),
        )
    if not human and (cost is None or cost > 0):
        # AI は 0 円・制約に触れない変更だけを適用できる
        raise _reject(RejectReason.FORBIDDEN, detail="paid changes need a human")
    if (
        payload.max_estimated_cost_usd is not None
        and cost is not None
        and cost > Decimal(str(payload.max_estimated_cost_usd))
    ):
        raise _reject(
            RejectReason.BUDGET_EXCEEDED,
            estimated_paid_cost_usd=str(cost),
            max_estimated_cost_usd=str(payload.max_estimated_cost_usd),
        )
    view = await budget_view(ctx.session, ctx.episode_id, ctx.budget_policy, ctx.now)
    refusal = check_additional_cost(view, cost)
    if refusal is not None:
        raise _reject(
            refusal,
            estimated_paid_cost_usd=None if cost is None else str(cost),
            budget=view.as_dict(),
        )


async def _write_script(ctx: ApplyContext, prepared: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    assert ctx.artifact_store is not None
    edit: _ScriptEdit = prepared["edit"]
    base: ArtifactMetadataRow = prepared["base_row"]
    digest = sha256_hex(canonical_json_bytes(edit.payload))
    put = await ctx.artifact_store.put_json(
        artifact_object_key(ctx.episode_id, ArtifactType.SCRIPT.value, digest), edit.payload
    )
    meta = await ArtifactMetadataRepository(ctx.session).record(
        episode_id=ctx.episode_id,
        artifact_type=ArtifactType.SCRIPT,
        schema_version=base.schema_version,
        bucket=base.bucket,
        object_key=put.key,
        sha256=put.sha256,
        size_bytes=put.size,
        # 台本工程の再利用キーを引き継ぐ: 台本工程の再実行が人の修正を生成で上書きしない
        input_hash=base.input_hash,
    )
    store = ctx.store
    cause = ctx.command.command_id
    await store.add_link(
        ctx.episode_id, base.id, meta.id, RevisionLinkKind.PARENT, causation_command_id=cause
    )
    storyboard = await _current_row(ctx, ArtifactType.STORYBOARD)
    if storyboard is not None:
        # シーン構成と尺は不変（build_edited_script が検査済み）。storyboard を作り直さず結び直す
        await store.add_link(
            ctx.episode_id,
            meta.id,
            storyboard.id,
            RevisionLinkKind.REBOUND_TO,
            causation_command_id=cause,
        )
    return meta.id, {"script": edit.diff, "base_revision_id": str(base.id)}


async def _prepare_override(
    ctx: ApplyContext,
    change: SceneOverrideVisual,
    change_set: ChangeSet,
    scene: StoryboardScene,
    storyboard_row: ArtifactMetadataRow,
) -> dict[str, Any]:
    old_row, old_override = await _current_override(ctx, change.scene_id, storyboard_row)
    effective = apply_override(scene, old_override)
    if effective.visual_subject is None:
        raise _reject(
            RejectReason.UNSUPPORTED_CHANGE,
            detail="this storyboard predates visual_subject (ADR-0035); override not supported",
        )
    existing = [
        m
        for m in await ArtifactMetadataRepository(ctx.session).list_for_episode(ctx.episode_id)
        if m.artifact_type is ArtifactType.SCENE_VISUAL_OVERRIDE and m.scene_id == change.scene_id
    ]
    data = {
        "episode_id": ctx.episode_id,
        "type": ArtifactType.SCENE_VISUAL_OVERRIDE.value,
        "schema_version": "1.0",
        "source_storyboard": {
            "artifact_id": str(storyboard_row.id),
            "sha256": storyboard_row.sha256,
            "schema_version": storyboard_row.schema_version,
        },
        "scene_id": change.scene_id,
        "revision": len(existing) + 1,
        "visual_kind": effective.visual_kind.value,
        "visual_subject": effective.visual_subject.value,
        "visual_description": change.visual_description,
        "framing": effective.framing,
        "camera_movement": effective.camera_movement,
        "rationale": (change_set.rationale or f"change set {change_set.change_set_id}")[:2000],
        "rejection_ids": [],
        "planner": {
            "generator": INTERVENTION_GENERATOR,
            "generator_model": change_set.actor.type.value,
            "generation_profile_id": INTERVENTION_PROFILE,
        },
        "change_set_id": change_set.change_set_id,
    }
    try:
        artifact = SceneVisualOverrideArtifact.model_validate(data).model_dump(mode="json")
    except ValidationError as exc:
        raise _reject(RejectReason.INVALID_PAYLOAD, detail=str(exc)[:1000]) from None
    return {
        "artifact": artifact,
        "old_row": old_row,
        "before": effective.visual_description,
    }


async def _write_override(
    ctx: ApplyContext, change: SceneOverrideVisual, prepared: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    assert ctx.artifact_store is not None
    override = prepared["override"]
    storyboard_row: ArtifactMetadataRow = prepared["base_row"]
    artifact = override["artifact"]
    digest = sha256_hex(canonical_json_bytes(artifact))
    # キーは ADR-0035 の代替映像案と同じ規約（scene を挟まない）
    put = await ctx.artifact_store.put_json(
        artifact_object_key(ctx.episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE.value, digest),
        artifact,
    )
    meta = await ArtifactMetadataRepository(ctx.session).record(
        episode_id=ctx.episode_id,
        artifact_type=ArtifactType.SCENE_VISUAL_OVERRIDE,
        schema_version="1.0",
        bucket=storyboard_row.bucket,
        object_key=put.key,
        sha256=put.sha256,
        size_bytes=put.size,
        scene_id=change.scene_id,
    )
    cause = ctx.command.command_id
    old_row: ArtifactMetadataRow | None = override["old_row"]
    if old_row is not None and str(old_row.id) != meta.id:
        await ctx.store.add_link(
            ctx.episode_id, old_row.id, meta.id, RevisionLinkKind.PARENT, causation_command_id=cause
        )
    await ctx.store.add_link(
        ctx.episode_id,
        storyboard_row.id,
        meta.id,
        RevisionLinkKind.DERIVED_FROM,
        causation_command_id=cause,
    )
    diff = {
        "scene_id": change.scene_id,
        "visual_description": {"before": override["before"], "after": change.visual_description},
        "base_revision_id": str(storyboard_row.id),
    }
    return meta.id, diff


async def _record_decision(
    ctx: ApplyContext,
    change_set: ChangeSet,
    change: ScriptEditScenes | SceneOverrideVisual,
    result_revision_id: str,
    diff: dict[str, Any],
) -> None:
    """判断記録。actor は ChangeSet の作者（AI の提案を人の直接編集として記録しない）。

    適用した主体は ``command_id`` が指す command 行（actor・scope）に残る。
    """
    author = change_set.actor
    rationale_kind = (
        RationaleKind.NONE
        if not change_set.rationale
        else RationaleKind.HUMAN
        if author.type is ActorType.HUMAN
        else RationaleKind.REPORTED
    )
    payload = DecisionRecordPayload(
        decision_type="apply_change",
        question=f"apply change set {change_set.change_set_id} ({change.kind.value})",
        candidates=(
            DecisionCandidate(
                id=change_set.change_set_id,
                summary=f"{change.kind.value} on {change.base_revision_id}",
            ),
        ),
        selected_candidate=change_set.change_set_id,
        rationale_kind=rationale_kind,
        rationale=change_set.rationale,
        target_revisions=(change.base_revision_id,),
        result_revision=result_revision_id,
        override_command_id=ctx.command.command_id,
        override_diff=diff,
    )
    await ctx.store.insert_decision(
        idempotency_key=f"command:{ctx.command.command_id}:apply_change",
        decision_type="apply_change",
        actor=author,
        payload=payload.model_dump(mode="json"),
        episode_id=ctx.episode_id,
        result_revision_id=result_revision_id,
        command_id=ctx.command.command_id,
        correlation_id=ctx.command.correlation_id,
    )


async def _close_variants(ctx: ApplyContext, applied: ChangeSet) -> None:
    if applied.variant_group is None:
        return
    others = [
        cs
        for cs in await ctx.store.list_change_sets(ctx.episode_id)
        if cs.variant_group == applied.variant_group
        and cs.change_set_id != applied.change_set_id
        and cs.status is ChangeSetStatus.PROPOSED
    ]
    for other in others:
        await ctx.store.set_change_set_status(
            other.change_set_id,
            ChangeSetStatus.NOT_SELECTED,
            decided_by_command_id=ctx.command.command_id,
        )
    await ctx.store.append_event(
        ctx.episode_id,
        InterventionEvent.VARIANT_SELECTED,
        ctx.actor,
        {
            "variant_group": applied.variant_group,
            "selected": applied.change_set_id,
            "not_selected": [o.change_set_id for o in others],
        },
        command_id=ctx.command.command_id,
    )
