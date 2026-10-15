"""判断の説明と Episode 横断の判断検索（ADR-0042 §5）。どちらも scope ``read`` の GET。

読み取り専用で LLM を呼ばない（INV-16）。説明は保存済みの記録だけから組み立てる
（``infrastructure/intervention/explain.py``）。認証・scope は介入 API と同じ依存を使う。
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.dependencies import get_session_factory
from apps.api.routers.intervention import PREFIX, Reader
from contracts.intervention import AgentRole
from infrastructure.intervention.explain import (
    DEFAULT_SEARCH_LIMIT,
    MAX_SEARCH_LIMIT,
    DecisionExplanation,
    DecisionPage,
    InvalidCursorError,
    explain_decision,
    search_decisions,
)

router = APIRouter(prefix=PREFIX, tags=["intervention"])


def get_insights_session_factory() -> async_sessionmaker[AsyncSession]:
    """テストは override する。認証の後に解決される。"""
    return get_session_factory()


SessionFactory = Annotated[async_sessionmaker[AsyncSession], Depends(get_insights_session_factory)]


def _error(code: int, error: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"error": error, "message": message})


@router.get(
    "/episodes/{episode_id}/decisions/{decision_id}/explanation",
    response_model=DecisionExplanation,
)
async def get_decision_explanation(
    episode_id: uuid.UUID, decision_id: uuid.UUID, _: Reader, sessions: SessionFactory
) -> DecisionExplanation:
    async with sessions() as session:
        explanation = await explain_decision(session, str(episode_id), str(decision_id))
    if explanation is None:
        raise _error(
            status.HTTP_404_NOT_FOUND,
            "decision_not_found",
            f"decision {decision_id} not found in this episode",
        )
    return explanation


@router.get("/decisions", response_model=DecisionPage)
async def search_decision_history(
    _: Reader,
    sessions: SessionFactory,
    decision_type: Annotated[str | None, Query(max_length=64)] = None,
    role: Annotated[AgentRole | None, Query()] = None,
    episode_id: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SEARCH_LIMIT)] = DEFAULT_SEARCH_LIMIT,
    before: Annotated[
        str | None, Query(max_length=64, description="前ページの next_before")
    ] = None,
) -> Any:
    async with sessions() as session:
        try:
            return await search_decisions(
                session,
                decision_type=decision_type,
                role=role,
                episode_id=None if episode_id is None else str(episode_id),
                limit=limit,
                before=before,
            )
        except InvalidCursorError:
            raise _error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "invalid_cursor",
                "before must be the next_before of a previous page",
            ) from None
