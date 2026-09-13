"""Episodeエンドポイント。

ハンドラの責務は「検証 → 永続化 → workflow起動 → 202」だけ。
重い処理をここで待たない（INV-16）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies import get_session_factory, get_workflow_starter
from apps.api.schemas import (
    ArtifactView,
    CreateEpisodeRequest,
    CreateEpisodeResponse,
    EpisodeView,
    JobView,
    StartStoryboardResponse,
)
from apps.api.workflow_starter import WorkflowStarter
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)

router = APIRouter(prefix="/episodes", tags=["episodes"])

SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
Starter = Annotated[WorkflowStarter, Depends(get_workflow_starter)]


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=CreateEpisodeResponse)
async def create_episode(
    payload: CreateEpisodeRequest,
    session_factory: SessionFactory,
    starter: Starter,
) -> CreateEpisodeResponse:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic=payload.topic)
        await session.commit()

    workflow_id = await starter.start_episode_workflow(
        episode_id=episode.id, pipeline=payload.pipeline
    )

    async with session_factory() as session:
        # Temporal参照は相関のためだけに持つ。状態の権威はDB（INV-7 / INV-8）。
        await EpisodeRepository(session).set_workflow_id(episode.id, workflow_id)
        await session.commit()

    return CreateEpisodeResponse(id=episode.id, status=episode.status, workflow_id=workflow_id)


@router.post(
    "/{episode_id}/storyboard",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartStoryboardResponse,
)
async def start_storyboard(
    episode_id: uuid.UUID,
    session_factory: SessionFactory,
    starter: Starter,
) -> StartStoryboardResponse:
    """storyboard 工程を起動するだけ（INV-16）。

    状態の前提（``script_ready`` / ``storyboard_ready``）は workflow の admit Activity が
    判定する。ここで判定すると、判定と起動の間に状態が変わる窓を API が抱えるため。
    """
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")

    workflow_id = await starter.start_storyboard_workflow(episode_id=episode.id)
    return StartStoryboardResponse(
        episode_id=episode.id, status=episode.status, workflow_id=workflow_id
    )


@router.get("/{episode_id}", response_model=EpisodeView)
async def get_episode(
    episode_id: uuid.UUID,
    session_factory: SessionFactory,
) -> EpisodeView:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        if episode is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")

        jobs = await JobRepository(session).list_for_episode(episode_id)
        artifacts = await ArtifactMetadataRepository(session).list_for_episode(episode_id)

    return EpisodeView(
        id=episode.id,
        status=episode.status,
        topic=episode.topic,
        created_at=episode.created_at,
        updated_at=episode.updated_at,
        jobs=[
            JobView(
                id=job.id,
                type=job.type,
                status=job.status,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                created_at=job.created_at,
                updated_at=job.updated_at,
            )
            for job in jobs
        ],
        artifacts=[
            ArtifactView(
                id=a.id,
                artifact_type=a.artifact_type,
                schema_version=a.schema_version,
                bucket=a.bucket,
                object_key=a.object_key,
                sha256=a.sha256,
                created_at=a.created_at,
            )
            for a in artifacts
        ],
    )
