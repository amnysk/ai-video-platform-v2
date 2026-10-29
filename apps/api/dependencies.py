"""FastAPIの依存。テストからは override して実サービスへ到達させない（INV-18）。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.workflow_starter import TemporalWorkflowStarter, WorkflowStarter
from domain.quality.deterministic import DEFAULT_PROVIDERS, CheckProvider
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return session_factory_from_settings(get_settings())


async def get_workflow_starter() -> WorkflowStarter:  # pragma: no cover - 実接続経路
    return await TemporalWorkflowStarter.connect(get_settings())


def get_quality_providers() -> Sequence[CheckProvider]:
    """手動投稿の品質判定が使う検査の列（ADR-0076 §4）。自動経路と同じ既定。テストが差し替える。"""
    return DEFAULT_PROVIDERS


def get_quality_clock() -> Callable[[], datetime]:
    return lambda: datetime.now(UTC)
