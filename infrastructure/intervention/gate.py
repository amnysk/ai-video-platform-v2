"""dispatch gate（ADR-0041 §2、``docs/domain/intervention.md`` §3）。

有料 provider と投稿の5経路は、外部呼び出しの直前に
``ProviderReservationRepository.mark_dispatched`` / ``mark_upload_dispatched`` の条件付き UPDATE を
commit する。gate はその**同じ transaction**で、条件付き UPDATE の前に呼ばれる。

- ``episode_controls`` 行を ``FOR SHARE`` で読む（PostgreSQL）。hold の受付は同じ行を
  ``UPDATE`` するので、dispatch と hold は行 lock で前後が決まる（INV-44）
- ``control_state != running`` → ``EpisodeHeldError``（予約は reserved かつ未 dispatch のまま残る）
- ``mode != legacy`` で、現行の script / storyboard の revision に生成した agent_run も
  人の変更を表す decision_record も無い → ``MissingDecisionRecordError``（INV-46）
- 行の無い Episode（古い Episode・日次実行）は ``running`` / ``legacy`` として通す。**何も書かない**

このモジュールは ``infrastructure/db/repositories.py`` から import される。循環を避けるため
repositories を import せず、表を直接読む。
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import (
    CO_PRODUCTION_REVIEW_POINTS,
    AnnotationKind,
    ControlState,
    InterventionMode,
    ReviewPoint,
)
from contracts.states import ArtifactType
from domain.errors import EpisodeHeldError, MissingDecisionRecordError
from infrastructure.db.models import AgentRunRow, ArtifactMetadataRow, DecisionRecordRow
from infrastructure.intervention.store import EpisodeControl, InterventionStore

#: 待機地点を持つ運転モード（``docs/domain/intervention.md`` §2）
REVIEWING_MODES: frozenset[InterventionMode] = frozenset(
    {InterventionMode.REVIEW_POINTS, InterventionMode.CO_PRODUCTION}
)

#: 新方式で「判断・実行の記録」を必須にする成果物（``docs/domain/intervention.md`` §6）
RECORD_REQUIRED_TYPES: tuple[ArtifactType, ...] = (ArtifactType.SCRIPT, ArtifactType.STORYBOARD)


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def check_dispatch_allowed(session: AsyncSession, episode_id: uuid.UUID | str) -> None:
    """新しい有料 dispatch・投稿を始めてよいかを判定する。拒否は例外。commit しない。"""
    control = await InterventionStore(session).get_control(episode_id, lock="share")
    if not control.persisted:
        return
    if control.control_state is not ControlState.RUNNING:
        raise EpisodeHeldError(
            f"episode {episode_id} is {control.control_state.value}; "
            "no new paid dispatch or upload starts until it is resumed"
        )
    if control.mode is InterventionMode.LEGACY:
        return
    missing = await missing_required_records(session, episode_id)
    if missing:
        raise MissingDecisionRecordError(
            f"episode {episode_id} ({control.mode.value}) has no agent run or decision record "
            f"for the current {', '.join(missing)} revision; paid dispatch is refused"
        )


async def missing_required_records(session: AsyncSession, episode_id: uuid.UUID | str) -> list[str]:
    """現行の script / storyboard のうち、生成記録も人の変更記録も無いものの型名。"""
    rows = (
        await session.execute(
            select(ArtifactMetadataRow.id, ArtifactMetadataRow.artifact_type).where(
                ArtifactMetadataRow.episode_id == _uuid(episode_id),
                ArtifactMetadataRow.artifact_type.in_([t.value for t in RECORD_REQUIRED_TYPES]),
                ArtifactMetadataRow.scene_id.is_(None),
                ArtifactMetadataRow.superseded_at.is_(None),
            )
        )
    ).all()
    missing: list[str] = []
    for revision_id, artifact_type in rows:
        has_run = await session.scalar(
            select(exists().where(AgentRunRow.output_revision_id == revision_id))
        )
        if has_run:
            continue
        has_decision = await session.scalar(
            select(exists().where(DecisionRecordRow.result_revision_id == revision_id))
        )
        if not has_decision:
            missing.append(str(artifact_type))
    return sorted(missing)


# --------------------------------------------------------------------------- 待機地点（§2 / §5）


def effective_review_points(control: EpisodeControl) -> tuple[ReviewPoint, ...]:
    """この Episode で待つ地点。co_production で未指定なら既定の主要地点。"""
    if control.mode not in REVIEWING_MODES:
        return ()
    if control.mode is InterventionMode.CO_PRODUCTION and not control.review_points:
        return CO_PRODUCTION_REVIEW_POINTS
    return control.review_points


async def approval_is_current(
    store: InterventionStore,
    episode_id: uuid.UUID | str,
    point: ReviewPoint,
    manifest_id: str,
) -> bool:
    """``(point, manifest)`` に結び付いた、撤回されていない approval があるか（INV-45 の読み方）。

    approval は manifest に結び付く。manifest が変われば古い approval はここで一致しない
    （流用されない）。撤回は ``resolution`` 注記が approval を指す（元の行は消さない）。
    """
    annotations = await store.list_annotations(episode_id)
    withdrawn = {
        a.refers_to_annotation_id
        for a in annotations
        if a.kind is AnnotationKind.RESOLUTION and a.refers_to_annotation_id is not None
    }
    return any(
        a.kind is AnnotationKind.APPROVAL
        and a.manifest_id == manifest_id
        and (a.payload or {}).get("point") == point.value
        and a.annotation_id not in withdrawn
        for a in annotations
    )
