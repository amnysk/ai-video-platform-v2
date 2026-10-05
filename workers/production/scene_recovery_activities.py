"""拒否されたシーンの代替映像案を計画・保存する Activity（ADR-0035）。

順序（予約台帳は ADR-0013 と同じ書き込み順序）:

1. 入力を読む: storyboard（id 固定・sha256 照合）、台本（narration）、このシーンの拒否、
   過去の代替案。実効シーン = storyboard のシーン + 現行の代替案
2. 冪等: 現行の案が workflow の知る revision より新しければそれを返す（計画しない）
3. 未対処の拒否が無ければ計画しない（同じ案のまま再び止まった → needs_input）
4. 上限（INV-34）: 回数・追加費用を DB から数える。超えれば needs_input
5. planner 呼び出し（``CODEX_SCENE_ALTERNATIVE`` として予約 → dispatch → 生出力を evidence に
   保存 → spent）。同じ入力の予約が既に結果を持っていれば再利用し、呼び直さない
6. 解釈と検証（``domain.production.scene_alternative``）。不成立・検証失敗は needs_input
7. ``SCENE_VISUAL_OVERRIDE`` Artifact を保存（シーン単位の現行を supersede）し、予約へ紐づける

有料の画像・動画はここでは作らない。作り直しは workflow がこのシーンの画像から呼び直す。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from contracts.artifacts import (
    SceneVisualOverrideArtifact,
    ScriptArtifact,
    StoryboardArtifact,
    build_scene_visual_override_artifact,
    parse_scene_visual_override_artifact,
)
from contracts.log_contract import EventName, LogStage
from contracts.production_activities import (
    MAX_RECOVERY_COST_USD_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_SCENE,
    PLAN_SCENE_ALTERNATIVE,
    PlanSceneAlternativeRequest,
    SceneAlternativeOutcome,
)
from contracts.states import ArtifactType, ProviderCall, RejectionCategory, ReservationStatus
from domain.artifact.entities import ArtifactMetadata
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.errors import (
    ProductionInputInvalidError,
    ProductionInputMissingError,
    SceneAlternativeInfeasibleError,
    SceneAlternativeInvalidError,
    SceneAlternativeNotApplicableError,
    SceneAlternativePlannerUnavailableError,
    UnreconciledReservationError,
    classify_failure,
)
from domain.production.effective_scene import apply_override, scene_visual_fingerprint
from domain.production.identity import idempotency_key
from domain.production.scene_alternative import (
    CostEntry,
    PreviousAlternative,
    RejectionFact,
    SceneAlternativeContext,
    SceneAlternativeInfeasible,
    SceneAlternativePlanner,
    allowed_subjects,
    check_recovery_limits,
    parse_planner_output,
    recovery_cost_usd,
    scene_alternative_input_hash,
    validate_proposal,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    ProviderRejectionRecord,
    ProviderRejectionRepository,
    ProviderReservation,
    ProviderReservationRepository,
)
from infrastructure.logging.emit import emit, log_guard
from infrastructure.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

SCENE_ALTERNATIVE_RAW_PREFIX = "provider-raw"
_PROVIDER = ProviderCall.CODEX_SCENE_ALTERNATIVE


class SceneAlternativeActivities:
    """planner は注入する（INV-18）。``None`` なら計画せず needs_input（安全側）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        bucket: str,
        planner: SceneAlternativePlanner | None,
        max_alternatives_per_scene: int = MAX_SCENE_ALTERNATIVES_PER_SCENE,
        max_alternatives_per_episode: int = MAX_SCENE_ALTERNATIVES_PER_EPISODE,
        max_recovery_cost_usd: float = MAX_RECOVERY_COST_USD_PER_EPISODE,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._bucket = bucket
        self._planner = planner
        #: 上限（INV-34）は設定値（``Settings``）から worker が注入する。既定は contracts の1箇所
        self._max_per_scene = max_alternatives_per_scene
        self._max_per_episode = max_alternatives_per_episode
        self._max_cost_usd = max_recovery_cost_usd

    def all_activities(self) -> list[Callable[..., Any]]:
        return [self.plan_scene_alternative]

    @activity.defn(name=PLAN_SCENE_ALTERNATIVE)
    async def plan_scene_alternative(
        self, request: PlanSceneAlternativeRequest
    ) -> SceneAlternativeOutcome:
        return await self.plan(request)

    async def plan(self, request: PlanSceneAlternativeRequest) -> SceneAlternativeOutcome:
        storyboard_meta, storyboard = await self._load_storyboard(request)
        script = await self._load_script(storyboard)
        try:
            (original,) = [s for s in storyboard.scenes if s.scene_id == request.scene_id]
        except ValueError as exc:
            raise ProductionInputInvalidError(
                f"scene {request.scene_id} is not in storyboard {storyboard_meta.id}"
            ) from exc

        async with self._session_factory() as session:
            rejections = await ProviderRejectionRepository(session).list_for_scene(
                request.episode_id, request.scene_id
            )
            all_meta = await ArtifactMetadataRepository(session).list_for_episode(
                request.episode_id
            )
            costs = await ProviderRejectionRepository(session).list_paid_reservation_costs(
                request.episode_id
            )
            current_meta = await ArtifactMetadataRepository(session).find_current_by_type(
                request.episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE, request.scene_id
            )
        overrides_meta = [
            m for m in all_meta if m.artifact_type is ArtifactType.SCENE_VISUAL_OVERRIDE
        ]
        scene_overrides = sorted(
            (m for m in overrides_meta if m.scene_id == request.scene_id),
            key=lambda m: m.version,
        )
        loaded = [(m, await self._read_override(m)) for m in scene_overrides]
        current = next(
            ((m, o) for m, o in loaded if current_meta is not None and m.id == current_meta.id),
            None,
        )

        # (2) 冪等: workflow がまだ試していない案が既にあれば、それを返す
        if current is not None and current[1].revision > request.seen_revision:
            return _outcome(current[0], current[1], newly_planned=False)

        # (3) 未対処の拒否が無いのに再び止まった → 同じ案を繰り返させない
        addressed = {rid for _, o in loaded for rid in o.rejection_ids}
        if not rejections:
            raise SceneAlternativeInvalidError(
                f"scene {request.scene_id} has no recorded provider rejection to address"
            )
        # ADR-0035 (8): 代替映像案で直せるのは内容方針の拒否だけ。workflow は例外の型名で
        # 計画を頼むので（ProviderRejectedError は検証失敗・分類不能の 422 でも同じ型）、ここで
        # 分類を見る。未対処の失敗に内容方針以外が1件でもあれば、planner を呼ばずに止める
        pending = [r for r in rejections if r.id not in addressed]
        not_applicable = sorted(
            {
                r.category.value
                for r in pending
                if r.category is not RejectionCategory.CONTENT_POLICY
            }
        )
        if not_applicable:
            raise SceneAlternativeNotApplicableError(
                f"scene {request.scene_id} stopped on {', '.join(not_applicable)} failure(s), "
                "not a content-policy rejection; a different visual would not fix it, so no "
                "alternative is planned. A human needs to look at it"
            )
        unaddressed = pending
        if not unaddressed:
            raise SceneAlternativeInvalidError(
                f"scene {request.scene_id} was blocked again although the current alternative "
                f"(revision {current[1].revision if current else 0}) addresses every recorded "
                "rejection; a human needs to look at it"
            )

        # (4) 上限（INV-34）。回数・費用は DB から数えるので resume してもリセットされない
        first_alternative_at: dict[str, datetime] = {}
        for meta in overrides_meta:
            if meta.scene_id is None:
                continue
            seen = first_alternative_at.get(meta.scene_id)
            if seen is None or meta.created_at < seen:
                first_alternative_at[meta.scene_id] = meta.created_at
        entries = [
            CostEntry(c.scene_id, c.estimated_cost_usd, c.input_rejected_by_provider, c.reserved_at)
            for c in costs
        ]
        check_recovery_limits(
            scene_alternatives=len(scene_overrides),
            episode_alternatives=len(overrides_meta),
            recovery_cost=recovery_cost_usd(entries, first_alternative_at),
            projected_cost=_projected_rebuild_cost(costs, request.scene_id),
            max_per_scene=self._max_per_scene,
            max_per_episode=self._max_per_episode,
            max_cost_usd=self._max_cost_usd,
        )

        if self._planner is None:
            raise SceneAlternativePlannerUnavailableError(
                "no scene alternative planner is configured for this worker"
            )

        effective = apply_override(original, current[1] if current else None)
        context = SceneAlternativeContext(
            episode_id=request.episode_id,
            scene=effective,
            original_description=original.visual_description,
            narration=_narration(script, original.script_scene_id),
            language=script.language,
            rejections=tuple(_fact(r) for r in rejections),
            previous=tuple(
                PreviousAlternative(o.revision, o.visual_subject, o.visual_description)
                for _, o in loaded
            ),
            allowed_subjects=allowed_subjects(_fact(r) for r in rejections),
        )
        input_hash = scene_alternative_input_hash(
            episode_id=request.episode_id,
            scene_id=request.scene_id,
            scene_fingerprint=scene_visual_fingerprint(effective),
            rejection_ids=[r.id for r in unaddressed],
            previous_override_sha256s=[m.sha256 for m, _ in loaded],
            planner_profile_id=self._planner.generation_profile_id,
        )

        # (5) planner 呼び出し（予約台帳）
        reservation, raw_text = await self._call_planner(request, context, input_hash)
        if reservation.outcome_artifact_id is not None:
            async with self._session_factory() as session:
                meta = await ArtifactMetadataRepository(session).get(
                    reservation.outcome_artifact_id
                )
            if meta is not None:
                return _outcome(meta, await self._read_override(meta), newly_planned=False)

        # (6) 解釈と検証
        plan = parse_planner_output(raw_text)
        if isinstance(plan, SceneAlternativeInfeasible):
            raise SceneAlternativeInfeasibleError(
                f"no policy-compatible alternative for scene {request.scene_id} that keeps the "
                f"facts: {plan.reason}"
            )
        validate_proposal(plan, context)

        # (7) 保存
        revision = len(scene_overrides) + 1
        try:
            artifact = build_scene_visual_override_artifact(
                episode_id=request.episode_id,
                source_storyboard={
                    "artifact_id": storyboard_meta.id,
                    "sha256": storyboard_meta.sha256,
                    "schema_version": storyboard_meta.schema_version,
                },
                scene_id=request.scene_id,
                revision=revision,
                visual_kind=plan.visual_kind,
                visual_subject=plan.visual_subject,
                visual_description=plan.visual_description,
                framing=plan.framing,
                camera_movement=plan.camera_movement,
                rationale=plan.rationale,
                rejection_ids=[r.id for r in unaddressed],
                planner={
                    "generator": self._planner.generator_id,
                    "generator_model": self._planner.generator_model,
                    "generation_profile_id": self._planner.generation_profile_id,
                },
            )
        except Exception as exc:  # pydantic ValidationError（長さの上限など）
            raise SceneAlternativeInvalidError(f"alternative violates the contract: {exc}") from exc
        digest = sha256_hex(canonical_json_bytes(artifact))
        put = await self._store.put_json(
            artifact_object_key(
                request.episode_id, ArtifactType.SCENE_VISUAL_OVERRIDE.value, digest
            ),
            artifact,
        )
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=request.episode_id,
                artifact_type=ArtifactType.SCENE_VISUAL_OVERRIDE,
                schema_version="1.0",
                bucket=self._bucket,
                object_key=put.key,
                sha256=digest,
                size_bytes=put.size,
                input_hash=input_hash,
                scene_id=request.scene_id,
            )
            await ProviderReservationRepository(session).attach_artifact(reservation.id, meta.id)
            await session.commit()
        with log_guard():
            emit(
                logger,
                EventName.LOG_RECORD,
                logging.INFO,
                "scene alternative planned episode=%s scene=%s revision=%s subject=%s",
                request.episode_id,
                request.scene_id,
                revision,
                plan.visual_subject.value,
                scene_revision=revision,
                artifact_id=meta.id,
                artifact_type=ArtifactType.SCENE_VISUAL_OVERRIDE.value,
                stage=LogStage.SCENE_RECOVERY.value,
            )
        return _outcome(meta, parse_scene_visual_override_artifact(artifact), newly_planned=True)

    # ------------------------------------------------------------------ planner と台帳

    async def _call_planner(
        self,
        request: PlanSceneAlternativeRequest,
        context: SceneAlternativeContext,
        input_hash: str,
    ) -> tuple[ProviderReservation, str]:
        assert self._planner is not None
        key = idempotency_key(provider=_PROVIDER.value, input_hash=input_hash, round=1)
        raw_key = f"{SCENE_ALTERNATIVE_RAW_PREFIX}/{request.episode_id}/scene-alternative/{key}.txt"
        async with self._session_factory() as session:
            reservations = ProviderReservationRepository(session)
            stale = [
                row
                for row in await reservations.find_unreconciled(
                    episode_id=request.episode_id, provider=_PROVIDER, scene_id=request.scene_id
                )
                if row.idempotency_key != key
            ]
            if stale:
                raise UnreconciledReservationError(
                    f"unreconciled reservation {stale[0].id} blocks a new scene alternative call"
                )
            reservation = await reservations.find_by_key(key)
            if reservation is None:
                reservation = await reservations.reserve(
                    episode_id=request.episode_id,
                    provider=_PROVIDER,
                    idempotency_key=key,
                    input_hash=input_hash,
                    round=1,
                    scene_id=request.scene_id,
                )
                await session.commit()

        if reservation.raw_output_key is not None:
            return reservation, await self._store.get_text(reservation.raw_output_key)
        if reservation.status is not ReservationStatus.RESERVED:
            raise SceneAlternativePlannerUnavailableError(
                f"planner call {reservation.id} already ended without output: "
                f"{reservation.error_summary or 'unknown'}"
            )
        if reservation.dispatched_at is not None:
            if await self._store.exists(raw_key):
                async with self._session_factory() as session:
                    reservation = await ProviderReservationRepository(session).mark_spent(
                        reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
                    )
                    await session.commit()
                return reservation, await self._store.get_text(raw_key)
            raise UnreconciledReservationError(
                f"planner reservation {reservation.id} was dispatched without evidence"
            )

        async with self._session_factory() as session:
            await ProviderReservationRepository(session).mark_dispatched(reservation.id)
            await session.commit()
        try:
            raw = await self._planner.plan(context)
        except Exception as exc:
            async with self._session_factory() as session:
                await ProviderReservationRepository(session).mark_spent(
                    reservation.id,
                    raw_output_key=None,
                    reconciled_by="conservative",
                    failure_class=classify_failure(exc),
                    error_summary=f"{type(exc).__name__}: {exc}",
                )
                await session.commit()
            raise SceneAlternativePlannerUnavailableError(
                f"scene alternative planner failed: {type(exc).__name__}: {exc}"
            ) from exc
        await self._store.put_text(raw_key, raw.text)
        async with self._session_factory() as session:
            reservation = await ProviderReservationRepository(session).mark_spent(
                reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
            )
            await session.commit()
        return reservation, raw.text

    # ------------------------------------------------------------------ 入力

    async def _load_storyboard(
        self, request: PlanSceneAlternativeRequest
    ) -> tuple[ArtifactMetadata, StoryboardArtifact]:
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(request.storyboard_artifact_id)
        if meta is None or meta.artifact_type is not ArtifactType.STORYBOARD:
            raise ProductionInputMissingError(
                f"storyboard {request.storyboard_artifact_id} not found"
            )
        payload = await self._verified_json(meta)
        return meta, StoryboardArtifact.model_validate(payload)

    async def _load_script(self, storyboard: StoryboardArtifact) -> ScriptArtifact:
        async with self._session_factory() as session:
            meta = await ArtifactMetadataRepository(session).get(
                storyboard.source_script.artifact_id
            )
        if meta is None:
            raise ProductionInputMissingError(
                f"script {storyboard.source_script.artifact_id} not found"
            )
        return ScriptArtifact.model_validate(await self._verified_json(meta))

    async def _read_override(self, meta: ArtifactMetadata) -> SceneVisualOverrideArtifact:
        return parse_scene_visual_override_artifact(await self._verified_json(meta))

    async def _verified_json(self, meta: ArtifactMetadata) -> dict[str, Any]:
        payload = await self._store.get_json(meta.object_key)
        if sha256_hex(canonical_json_bytes(payload)) != meta.sha256:
            raise ProductionInputInvalidError(f"{meta.object_key} sha256 mismatch")
        return payload


def _fact(record: ProviderRejectionRecord) -> RejectionFact:
    return RejectionFact(
        id=record.id,
        rejected_input=record.rejected_input,
        types=record.types,
        reason=record.reason,
        message=record.message,
        category=record.category,
    )


def _narration(script: ScriptArtifact, script_scene_id: str) -> str:
    for scene in script.scenes:
        if scene.id == script_scene_id:
            return scene.narration
    raise ProductionInputInvalidError(f"script scene {script_scene_id} not found")


def _projected_rebuild_cost(costs: list[Any], scene_id: str) -> Decimal:
    """次の作り直し（このシーンの画像 + 動画）の見積り。直近の同種予約の額を使う。"""
    latest: dict[ProviderCall, tuple[datetime, Decimal]] = {}
    for cost in costs:
        if cost.scene_id != scene_id:
            continue
        seen = latest.get(cost.provider)
        if seen is None or cost.reserved_at > seen[0]:
            latest[cost.provider] = (cost.reserved_at, cost.estimated_cost_usd)
    return sum((value for _, value in latest.values()), Decimal("0"))


def _outcome(
    meta: ArtifactMetadata, override: SceneVisualOverrideArtifact, *, newly_planned: bool
) -> SceneAlternativeOutcome:
    return SceneAlternativeOutcome(
        override_artifact_id=meta.id,
        revision=override.revision,
        visual_subject=override.visual_subject.value,
        newly_planned=newly_planned,
    )
