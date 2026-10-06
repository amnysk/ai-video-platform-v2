"""採用 manifest（active_revision_set）の導出と初期化（ADR-0041 §1、設計 §5/§6）。

既存の worker が読む「現行」は ``artifact_metadata.superseded_at IS NULL``。新方式の採用 manifest は
その時点の現行から作り、以後の採用の変更は command の適用が**同じ transaction で**現行と manifest の
両方を更新する（``docs/domain/intervention.md`` §1）。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.db.models import ArtifactMetadataRow
from infrastructure.intervention.store import (
    Actor,
    ControlChange,
    InterventionStore,
    Manifest,
    manifest_slot,
)


async def current_members(session: AsyncSession, episode_id: uuid.UUID | str) -> dict[str, str]:
    """現行（``superseded_at IS NULL``）の成果物を slot → revision_id で返す。"""
    episode_uuid = episode_id if isinstance(episode_id, uuid.UUID) else uuid.UUID(str(episode_id))
    rows = await session.scalars(
        select(ArtifactMetadataRow)
        .where(
            ArtifactMetadataRow.episode_id == episode_uuid,
            ArtifactMetadataRow.superseded_at.is_(None),
        )
        .order_by(ArtifactMetadataRow.artifact_type, ArtifactMetadataRow.scene_id)
    )
    return {manifest_slot(row.artifact_type, row.scene_id): str(row.id) for row in rows}


async def ensure_manifest(
    session: AsyncSession, store: InterventionStore, episode_id: uuid.UUID | str, actor: Actor
) -> Manifest:
    """採用 manifest を返す。無ければ現行の成果物から作り、制御行の採用ポインタを設定する。

    最初の manifest は「現行の記録」で採用の変更ではないので、制御版を増やさない。
    現行が採用 manifest と食い違っていたら（旧経路の工程が成果物を作った）、
    新しい manifest を足して追従する。追従は介入ではないのでイベントを足さない
    （manifest 行に causation が無いことで区別できる）。commit は呼び出し側。
    """
    control = await store.lock_control(episode_id)
    members = await current_members(session, episode_id)
    if control.active_manifest_id is not None:
        active = await store.get_manifest(control.active_manifest_id)
        if active is not None and active.members == members:
            return active
        parent = None if active is None else active.manifest_id
    else:
        parent = None
    manifest = await store.insert_manifest(
        episode_id, members, actor=actor, parent_manifest_id=parent
    )
    await store.update_control(
        episode_id,
        ControlChange(active_manifest_id=manifest.manifest_id, bump_revision=False),
    )
    return manifest
