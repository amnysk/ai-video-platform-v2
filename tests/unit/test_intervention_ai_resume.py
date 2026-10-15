"""AI の resume / release_checkpoint は有料の生成を伴う実行を始められない（ADR-0041 addendum）。

apply_change は AI に有料の変更を禁じている。resume が降ろされた出力を作り直す（課金する）ので、
同じ規則を resume にも課す。人は影響を受けない。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import update

from contracts.intervention import CommandStatus, CommandType, ForbiddenDetail, RejectReason
from contracts.states import ArtifactType, EpisodeStatus
from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.intervention.store import InterventionStore
from tests.support.intervention import (
    AI,
    HUMAN,
    apply,
    envelope,
    make_episode,
    receive,
    seed_script_and_storyboard,
    submit,
)


async def _retire(
    session_factory, episode_id: str, *slots: tuple[ArtifactType, str | None]
) -> None:
    async with session_factory() as s:
        for artifact_type, scene_id in slots:
            stmt = (
                update(ArtifactMetadataRow)
                .where(
                    ArtifactMetadataRow.episode_id == uuid.UUID(episode_id),
                    ArtifactMetadataRow.artifact_type == artifact_type.value,
                    ArtifactMetadataRow.superseded_at.is_(None),
                )
                .values(superseded_at=datetime.now(UTC))
            )
            if scene_id is not None:
                stmt = stmt.where(ArtifactMetadataRow.scene_id == scene_id)
            await s.execute(stmt)
        await s.commit()


async def _held_episode(session, session_factory, artifact_store) -> str:
    episode_id = await make_episode(session, EpisodeStatus.ASSETS_READY)
    await seed_script_and_storyboard(session, artifact_store, episode_id)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    return episode_id


async def _resume(session_factory, artifact_store, episode_id: str, principal):
    env = envelope(CommandType.RESUME)
    await receive(session_factory, episode_id, env, principal)
    return await apply(session_factory, artifact_store, env.command_id)


async def test_ai_resume_that_only_remakes_voice_and_render_is_applied(
    session, session_factory, artifact_store
) -> None:
    episode_id = await _held_episode(session, session_factory, artifact_store)
    await _retire(
        session_factory,
        episode_id,
        (ArtifactType.SCENE_VOICE, "sb1"),
        (ArtifactType.PRODUCTION_MANIFEST, None),
    )
    outcome = await _resume(session_factory, artifact_store, episode_id, AI)
    assert outcome.status is CommandStatus.APPLIED
    assert outcome.pipeline_start is not None
    assert outcome.pipeline_start["start_stage"] == "production"


async def test_ai_resume_that_would_pay_for_a_missing_image_is_forbidden_with_a_detail_code(
    session, session_factory, artifact_store
) -> None:
    episode_id = await _held_episode(session, session_factory, artifact_store)
    await _retire(
        session_factory,
        episode_id,
        (ArtifactType.SCENE_IMAGE, "sb2"),
        (ArtifactType.PRODUCTION_MANIFEST, None),
    )
    outcome = await _resume(session_factory, artifact_store, episode_id, AI)
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.reject_reason is RejectReason.FORBIDDEN
    assert outcome.result["detail"] == ForbiddenDetail.AI_RESUME_REQUIRES_PAID_GENERATION.value
    assert outcome.pipeline_start is None
    async with session_factory() as s:
        control = await InterventionStore(s).get_control(episode_id)
    assert control.control_state.value == "held"  # 状態は変えない


async def test_human_resume_with_a_missing_image_is_applied(
    session, session_factory, artifact_store
) -> None:
    episode_id = await _held_episode(session, session_factory, artifact_store)
    await _retire(
        session_factory,
        episode_id,
        (ArtifactType.SCENE_IMAGE, "sb2"),
        (ArtifactType.PRODUCTION_MANIFEST, None),
    )
    outcome = await _resume(session_factory, artifact_store, episode_id, HUMAN)
    assert outcome.status is CommandStatus.APPLIED
    assert outcome.pipeline_start is not None


async def test_ai_resume_from_a_missing_storyboard_is_forbidden(
    session, session_factory, artifact_store
) -> None:
    episode_id = await _held_episode(session, session_factory, artifact_store)
    await _retire(
        session_factory,
        episode_id,
        (ArtifactType.STORYBOARD, None),
        (ArtifactType.PRODUCTION_MANIFEST, None),
    )
    outcome = await _resume(session_factory, artifact_store, episode_id, AI)
    assert outcome.status is CommandStatus.REJECTED
    assert outcome.result["detail"] == ForbiddenDetail.AI_RESUME_REQUIRES_PAID_GENERATION.value
