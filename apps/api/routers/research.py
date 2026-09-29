"""Research エンドポイント（ADR-0037 §8.5）。既存のエンドポイントは変えない（追加だけ）。

- ``POST /research/requests``: 検証 → ``ResearchGateway.submit``
  （冪等キー・鮮度キャッシュ・予算の門）→ ``queued`` なら ``ResearchWorkflow`` を起動 → 202。
  **完了は待たない**（INV-16）
- ``POST /research/requests/{id}/resume``: ``blocked → queued``（同じ門）→ 起動 → 202
- ``GET /research/requests/{id}``: DB だけを読む（Temporal に問い合わせない。INV-8）

保存と workflow 起動の間で落ちた依頼は ``queued`` のまま残る。同じ ``idempotency_key`` で
再 POST すれば同じ workflow id で起動し直す（実行中なら何もしない）。

Episode の API・工程はこのルータを呼ばない・待たない（INV-37）。``POST /episodes/{id}/script`` は
作らない（base INV-30 の統一再開と衝突する。ADR-0037 §9）。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies import get_session_factory, get_settings
from contracts.research import ResearchKind, ResearchStatus, ResearchSubmit
from domain.research.entities import ResearchRequest
from domain.research.errors import ResearchIdempotencyConflictError
from infrastructure.config import Settings
from infrastructure.db.research_repositories import ResearchRequestRepository
from infrastructure.research.gateway import GatewayConfig, ResearchGateway
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.research_starter import (
    ResearchWorkflowStarter,
    TemporalResearchStarter,
)

router = APIRouter(prefix="/research", tags=["research"])


@lru_cache(maxsize=1)
def _minio_store() -> MinioArtifactStore:  # pragma: no cover - 実接続経路
    return MinioArtifactStore.from_settings(get_settings())


def get_research_store() -> ArtifactStore:  # pragma: no cover - 実接続経路
    """鮮度キャッシュの再利用前の検証（読み戻し）に使う。テストは override する。"""
    return _minio_store()


async def get_research_starter() -> ResearchWorkflowStarter:  # pragma: no cover - 実接続経路
    return await TemporalResearchStarter.connect(get_settings())


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]
AppSettings = Annotated[Settings, Depends(get_settings)]
Store = Annotated[ArtifactStore, Depends(get_research_store)]
Starter = Annotated[ResearchWorkflowStarter, Depends(get_research_starter)]


class ResearchAcceptedResponse(BaseModel):
    request_id: str
    kind: ResearchKind
    status: ResearchStatus
    #: 鮮度内の完了済み依頼を（検証の上で）再利用した。新しく保存も実行もしていない
    reused: bool = False
    #: 相関用。起動しなかった（``blocked`` / 再利用 / 実行中・終了済み）なら ``None``。
    #: 状態の権威は DB
    workflow_id: str | None = None


class ResearchRequestView(BaseModel):
    request_id: str
    kind: ResearchKind
    status: ResearchStatus
    requester: str
    channel_id: str
    episode_id: str | None
    as_of: datetime
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    #: ``blocked`` の理由（``{"code", "detail"}``）
    blocked_reason: dict[str, Any] | None
    #: ``research_requests.result_summary``（``ResearchResult`` の形。成果物は参照だけ）
    result: dict[str, Any] | None


def _view(request: ResearchRequest) -> ResearchRequestView:
    return ResearchRequestView(
        request_id=request.id,
        kind=request.kind,
        status=request.status,
        requester=request.requester,
        channel_id=request.channel_id,
        episode_id=request.episode_id,
        as_of=request.as_of,
        created_at=request.created_at,
        updated_at=request.updated_at,
        started_at=request.started_at,
        finished_at=request.finished_at,
        blocked_reason=request.blocked_reason,
        result=request.result_summary,
    )


def _gateway(
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore, settings: Settings
) -> ResearchGateway:
    return ResearchGateway(
        session_factory=session_factory,
        store=store,
        config=GatewayConfig.from_settings(settings),
    )


def _accepted(
    request: ResearchRequest, *, reused: bool = False, workflow_id: str | None = None
) -> ResearchAcceptedResponse:
    return ResearchAcceptedResponse(
        request_id=request.id,
        kind=request.kind,
        status=request.status,
        reused=reused,
        workflow_id=workflow_id,
    )


@router.post(
    "/requests", status_code=status.HTTP_202_ACCEPTED, response_model=ResearchAcceptedResponse
)
async def submit_research(
    payload: ResearchSubmit,
    session_factory: SessionFactory,
    settings: AppSettings,
    store: Store,
    starter: Starter,
) -> ResearchAcceptedResponse:
    gateway = _gateway(session_factory, store, settings)
    try:
        result = await gateway.submit(payload)
    except ResearchIdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    request = result.request
    workflow_id: str | None = None
    if not result.reused and request.status is ResearchStatus.QUEUED:
        workflow_id = await starter.start_research(request_id=request.id)
    return _accepted(request, reused=result.reused, workflow_id=workflow_id)


@router.post(
    "/requests/{request_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ResearchAcceptedResponse,
)
async def resume_research(
    request_id: uuid.UUID,
    session_factory: SessionFactory,
    settings: AppSettings,
    store: Store,
    starter: Starter,
) -> ResearchAcceptedResponse:
    """``blocked`` の依頼を ``queued`` に戻して起動する。``blocked`` 以外・門を通らない依頼は 409。

    Provider が未設定のまま・凍結した上限が足りない依頼は再開しない（再開しても同じ理由で止まる）。
    """
    gateway = _gateway(session_factory, store, settings)
    result = await gateway.resume(str(request_id))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    if not result.resumed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"request {result.request.id} was not resumed: {result.reason} "
                f"(status={result.request.status.value})"
            ),
        )
    workflow_id = await starter.start_research(request_id=result.request.id)
    return _accepted(result.request, workflow_id=workflow_id)


@router.get("/requests/{request_id}", response_model=ResearchRequestView)
async def get_research_request(
    request_id: uuid.UUID, session_factory: SessionFactory
) -> ResearchRequestView:
    """DB だけを読む（Temporal に問い合わせない）。"""
    async with session_factory() as session:
        request = await ResearchRequestRepository(session).get(request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="request not found")
    return _view(request)
