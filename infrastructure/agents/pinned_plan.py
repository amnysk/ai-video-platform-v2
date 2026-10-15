"""Episode に pin 済みの agentic 計画を読む（resume が「agentic か」を決める材料）。

「agentic か」の判定は I/O 側（Activity / API）で行う。workflow は結果だけで分岐する。
pin された計画（``pin_workflow_plan`` が書く ``workflow_plan``）が無ければ ``None``（legacy）。
metadata の行だけで無いと分かる Episode は ArtifactStore に触らない。
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agentic import WorkflowPlan
from contracts.agentic_pipeline import PinnedAgenticTemplate
from contracts.states import ArtifactType
from infrastructure.agents.episode_artifacts import read_current_artifact
from infrastructure.db.repositories import ArtifactMetadataRepository
from infrastructure.storage.artifact_store import ArtifactStore


async def read_pinned_agentic_template(
    session_factory: async_sessionmaker[AsyncSession],
    store: Callable[[], ArtifactStore],
    episode_id: uuid.UUID | str,
) -> PinnedAgenticTemplate | None:
    async with session_factory() as session:
        meta = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.WORKFLOW_PLAN
        )
        if meta is None:
            return None
        current = await read_current_artifact(
            session, store(), episode_id, ArtifactType.WORKFLOW_PLAN
        )
    if current is None:
        return None
    plan = current[1]
    assert isinstance(plan, WorkflowPlan)
    return PinnedAgenticTemplate(plan.template_id, plan.template_version)


async def has_unstarted_agentic_draft(
    session_factory: async_sessionmaker[AsyncSession],
    episode_id: uuid.UUID | str,
) -> bool:
    """agentic の draft か（brief はあるが計画が未 pin = ``start_agentic_production`` 前）。

    ``read_pinned_agentic_template`` は draft を legacy と区別できない。resume は draft を
    legacy で走らせてはならないので、起動側が先にこれで断る。metadata の行だけを見る。
    """
    async with session_factory() as session:
        repo = ArtifactMetadataRepository(session)
        if await repo.find_current_by_type(episode_id, ArtifactType.USER_BRIEF) is None:
            return False
        return await repo.find_current_by_type(episode_id, ArtifactType.WORKFLOW_PLAN) is None
