"""Episodeエンドポイント。

ハンドラの責務は「検証 → 永続化 → workflow起動 → 202」だけ。
重い処理をここで待たない（INV-16）。
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError

from apps.api.dependencies import get_session_factory, get_workflow_starter
from apps.api.schemas import (
    ArtifactView,
    CreateEpisodeRequest,
    CreateEpisodeResponse,
    EpisodeView,
    JobView,
    StartProductionResponse,
    StartRenderRequest,
    StartRenderResponse,
    StartStoryboardResponse,
    StartUploadResponse,
)
from apps.api.workflow_starter import WorkflowStarter, render_workflow_id, upload_workflow_id
from contracts.render import DEFAULT_RENDER_PROFILE_ID, get_render_profile
from contracts.states import (
    PRODUCTION_ADMISSIBLE_STATUSES,
    RENDER_ADMISSIBLE_STATUSES,
    UPLOAD_ADMISSIBLE_STATUSES,
    EpisodeStatus,
)
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

    try:
        workflow_id = await starter.start_storyboard_workflow(episode_id=episode.id)
    except WorkflowAlreadyStartedError as exc:
        # 同じ Episode の storyboard workflow が実行中。二重起動しない（ADR-0015）。
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"storyboard workflow already running for episode {episode.id}",
        ) from exc
    return StartStoryboardResponse(
        episode_id=episode.id, status=episode.status, workflow_id=workflow_id
    )


@router.post(
    "/{episode_id}/production",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartProductionResponse,
)
async def start_production(
    episode_id: uuid.UUID,
    session_factory: SessionFactory,
    starter: Starter,
) -> StartProductionResponse:
    """production 工程を起動するだけ（INV-16 / ADR-0017）。

    入場の権威は workflow の admit Activity。ここでは明らかに入れない状態を早めに 409 で返す
    （``in_progress`` は閉じた run の引き継ぎがありうるので admit に任せる）。
    ``needs_work`` / ``blocked`` への POST は再試行 / 再開の操作になる。
    """
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")
    if (
        episode.status not in PRODUCTION_ADMISSIBLE_STATUSES
        and episode.status is not EpisodeStatus.IN_PROGRESS
    ):
        allowed = ", ".join(sorted(s.value for s in PRODUCTION_ADMISSIBLE_STATUSES))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"episode {episode.id} is {episode.status.value}; production can start only "
                f"from {allowed}"
            ),
        )

    try:
        workflow_id = await starter.start_production_workflow(episode_id=episode.id)
    except WorkflowAlreadyStartedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"production workflow already running for episode {episode.id}",
        ) from exc
    return StartProductionResponse(
        episode_id=episode.id, status=episode.status, workflow_id=workflow_id
    )


@router.post(
    "/{episode_id}/render",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartRenderResponse,
)
async def start_render(
    episode_id: uuid.UUID,
    session_factory: SessionFactory,
    starter: Starter,
    payload: Annotated[StartRenderRequest | None, Body()] = None,
) -> StartRenderResponse:
    """render 工程を起動するだけ（INV-16 / ADR-0019）。

    入場の権威は workflow の admit Activity。ここでは未知の profile（422）と、明らかに入れない状態
    （409）を早めに返す。profile は Activity でも再検証する。
    """
    profile_id = (payload.render_profile_id if payload else None) or DEFAULT_RENDER_PROFILE_ID
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")
    try:
        get_render_profile(profile_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"unknown render profile: {profile_id}",
        ) from exc
    if (
        episode.status not in RENDER_ADMISSIBLE_STATUSES
        and episode.status is not EpisodeStatus.IN_PROGRESS
    ):
        allowed = ", ".join(sorted(s.value for s in RENDER_ADMISSIBLE_STATUSES))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"episode {episode.id} is {episode.status.value}; render can start only "
                f"from {allowed}"
            ),
        )
    if episode.status in {EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED}:
        # 再開は render 自身が止めた Episode だけ（入場トークンの workflow id / 権威は admit）
        async with session_factory() as session:
            owner = await EpisodeRepository(session).get_workflow_id(episode.id)
        owner_workflow = (owner or "").rsplit(":", 1)[0]
        if owner_workflow != render_workflow_id(episode.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"episode {episode.id} is {episode.status.value} but was stopped by another "
                    f"stage ({owner_workflow or 'unknown'}); resume that stage instead"
                ),
            )

    try:
        workflow_id = await starter.start_render_workflow(
            episode_id=episode.id, render_profile_id=profile_id
        )
    except WorkflowAlreadyStartedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"render workflow already running for episode {episode.id}",
        ) from exc
    return StartRenderResponse(
        episode_id=episode.id,
        status=episode.status,
        workflow_id=workflow_id,
        render_profile_id=profile_id,
    )


@router.post(
    "/{episode_id}/upload",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=StartUploadResponse,
)
async def start_upload(
    episode_id: uuid.UUID,
    session_factory: SessionFactory,
    starter: Starter,
) -> StartUploadResponse:
    """upload 工程を起動するだけ（INV-16 / ADR-0020）。本文は受け取らない。

    入場の権威は workflow の admit Activity。ここでは明らかに入れない状態を早めに 409 で返す。
    ``uploaded`` は再投稿しないので 409（INV-14 / INV-19）。
    """
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    if episode is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="episode not found")
    if episode.status is EpisodeStatus.UPLOADED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"episode {episode.id} is already uploaded; it is never uploaded twice",
        )
    if (
        episode.status not in UPLOAD_ADMISSIBLE_STATUSES
        and episode.status is not EpisodeStatus.IN_PROGRESS
    ):
        allowed = ", ".join(sorted(s.value for s in UPLOAD_ADMISSIBLE_STATUSES))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"episode {episode.id} is {episode.status.value}; upload can start only "
                f"from {allowed}"
            ),
        )
    if episode.status in {EpisodeStatus.NEEDS_WORK, EpisodeStatus.BLOCKED}:
        async with session_factory() as session:
            owner = await EpisodeRepository(session).get_workflow_id(episode.id)
        owner_workflow = (owner or "").rsplit(":", 1)[0]
        if owner_workflow != upload_workflow_id(episode.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"episode {episode.id} is {episode.status.value} but was stopped by another "
                    f"stage ({owner_workflow or 'unknown'}); resume that stage instead"
                ),
            )
    try:
        workflow_id = await starter.start_upload_workflow(episode_id=episode.id)
    except WorkflowAlreadyStartedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"upload workflow already running for episode {episode.id}",
        ) from exc
    return StartUploadResponse(
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
