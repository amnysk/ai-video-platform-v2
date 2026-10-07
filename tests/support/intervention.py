"""介入 command のテスト支援（担当 B）。SQLite + InMemoryArtifactStore。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from contracts.artifacts import VisualSubject
from contracts.intervention import (
    ActorType,
    CommandEnvelope,
    CommandType,
    Scope,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.artifact.keys import artifact_object_key
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.commands import (
    CommandOutcome,
    Principal,
    ReceiveResult,
    apply_command,
    receive_command,
)
from infrastructure.intervention.store import Actor
from tests.support.production import sample_script, sample_storyboard

ALL_SCOPES = frozenset(Scope)
#: fal の見積り式の値（画像1枚・5秒の動画）。テストは adapter を import しない（INV-18）
IMAGE_USD = Decimal("0.0400")
VIDEO_USD = Decimal("1.2095")
HUMAN = Principal(Actor(ActorType.HUMAN, "owner"), ALL_SCOPES)
AI = Principal(Actor(ActorType.AI_ASSISTANT, "assistant"), ALL_SCOPES - {Scope.SET_MODE})
NOW = datetime(2026, 10, 7, 3, 0, tzinfo=UTC)

#: 状態 → そこへ至る事象の列
_PATH: dict[EpisodeStatus, tuple[EpisodeEvent, ...]] = {
    EpisodeStatus.PLANNED: (),
    EpisodeStatus.IN_PROGRESS: (EpisodeEvent.WORKFLOW_STARTED,),
    EpisodeStatus.SCRIPT_READY: (EpisodeEvent.WORKFLOW_STARTED, EpisodeEvent.SCRIPT_READY),
    EpisodeStatus.STORYBOARD_READY: (
        EpisodeEvent.WORKFLOW_STARTED,
        EpisodeEvent.SCRIPT_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.STORYBOARD_READY,
    ),
    EpisodeStatus.ASSETS_READY: (
        EpisodeEvent.WORKFLOW_STARTED,
        EpisodeEvent.SCRIPT_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.STORYBOARD_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.ASSETS_READY,
    ),
    EpisodeStatus.UPLOADED: (
        EpisodeEvent.WORKFLOW_STARTED,
        EpisodeEvent.SCRIPT_READY,
        EpisodeEvent.STAGE_ADMITTED,
        EpisodeEvent.UPLOAD_SUCCEEDED,
    ),
}


async def make_episode(session, status: EpisodeStatus = EpisodeStatus.PLANNED) -> str:
    repo = EpisodeRepository(session)
    episode = await repo.create(topic="縄文")
    for event in _PATH[status]:
        await repo.apply_event(episode.id, event)
    await session.commit()
    return episode.id


def envelope(
    type: CommandType,
    payload: dict[str, Any] | None = None,
    *,
    command_id: str | None = None,
    expected_control_revision: int | None = None,
    expected_target_revision: str | None = None,
) -> CommandEnvelope:
    return CommandEnvelope(
        command_id=command_id or f"cmd-{uuid.uuid4().hex[:16]}",
        type=type,
        payload=payload or {},
        expected_control_revision=expected_control_revision,
        expected_target_revision=expected_target_revision,
    )


async def receive(
    session_factory, episode_id: str, env: CommandEnvelope, principal: Principal = HUMAN
) -> ReceiveResult:
    async with session_factory() as session:
        result = await receive_command(session, episode_id, env, principal, now=NOW)
        await session.commit()
    return result


async def apply(
    session_factory, artifact_store, command_id: str, policy: BudgetPolicy | None = None
) -> CommandOutcome:
    return await apply_command(
        session_factory,
        artifact_store,
        command_id,
        budget_policy=policy or BudgetPolicy(),
        clock=lambda: NOW,
    )


async def submit(
    session_factory,
    artifact_store,
    episode_id: str,
    env: CommandEnvelope,
    principal: Principal = HUMAN,
    policy: BudgetPolicy | None = None,
) -> CommandOutcome:
    """受付 → 適用（command workflow の代わり）。"""
    await receive(session_factory, episode_id, env, principal)
    return await apply(session_factory, artifact_store, env.command_id, policy)


async def store_artifact(
    session,
    artifact_store,
    episode_id: str,
    artifact_type: ArtifactType,
    payload: dict[str, Any],
    *,
    scene_id: str | None = None,
    input_hash: str | None = None,
):
    digest = sha256_hex(canonical_json_bytes(payload))
    put = await artifact_store.put_json(
        artifact_object_key(episode_id, artifact_type.value, digest, scene_id), payload
    )
    meta = await ArtifactMetadataRepository(session).record(
        episode_id=episode_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        bucket="artifacts",
        object_key=put.key,
        sha256=put.sha256,
        size_bytes=put.size,
        scene_id=scene_id,
        input_hash=input_hash,
    )
    await session.commit()
    return meta


SUBJECTS = (VisualSubject.ARTIFACT, VisualSubject.SITE, VisualSubject.SITE, VisualSubject.MAP)


async def seed_script_and_storyboard(session, artifact_store, episode_id: str):
    """現行の script・storyboard（visual_subject つき）と、シーンごとの画像・動画・音声の行。"""
    script_payload = sample_script(episode_id).model_dump(mode="json")
    script = await store_artifact(
        session,
        artifact_store,
        episode_id,
        ArtifactType.SCRIPT,
        script_payload,
        input_hash="i" * 64,
    )
    board = sample_storyboard(
        episode_id, script_artifact_id=script.id, script_sha256=script.sha256
    ).model_dump(mode="json")
    for scene, subject in zip(board["scenes"], SUBJECTS, strict=True):
        scene["visual_subject"] = subject.value
    storyboard = await store_artifact(
        session, artifact_store, episode_id, ArtifactType.STORYBOARD, board
    )
    media: dict[str, Any] = {}
    for n in range(1, 5):
        sb = f"sb{n}"
        for artifact_type in (
            ArtifactType.SCENE_IMAGE,
            ArtifactType.SCENE_VIDEO,
            ArtifactType.SCENE_VOICE,
        ):
            meta = await store_artifact(
                session,
                artifact_store,
                episode_id,
                artifact_type,
                {"stand_in": artifact_type.value, "scene": sb},
                scene_id=sb,
                input_hash=f"{artifact_type.value}-{sb}".ljust(64, "0")[:64],
            )
            media[f"{artifact_type.value}:{sb}"] = meta
    # Production が送った画像・動画の予約（adapter が書いた見積り。精算済み）
    reservations = ProviderReservationRepository(session)
    for n in range(1, 5):
        for provider, cost in (
            (ProviderCall.FAL_IMAGE, IMAGE_USD),
            (ProviderCall.FAL_VIDEO, VIDEO_USD),
        ):
            row = await reservations.reserve(
                episode_id=episode_id,
                provider=provider,
                idempotency_key=f"{episode_id}:{provider.value}:sb{n}:1",
                input_hash="r" * 64,
                round=1,
                scene_id=f"sb{n}",
                estimated_cost_usd=cost,
            )
            await session.commit()
            await reservations.mark_dispatched(row.id)
            await reservations.record_provider_job_ref(row.id, f"job-{provider.value}-sb{n}")
            await reservations.mark_spent(row.id, raw_output_key=f"provider-raw/{row.id}")
            kind = "scene_image" if provider is ProviderCall.FAL_IMAGE else "scene_video"
            await reservations.attach_artifact(row.id, media[f"{kind}:sb{n}"].id)
            await session.commit()
    manifest = await store_artifact(
        session,
        artifact_store,
        episode_id,
        ArtifactType.PRODUCTION_MANIFEST,
        {"stand_in": "production_manifest"},
    )
    media["production_manifest"] = manifest
    return script, storyboard, media
