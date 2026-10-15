"""FastAPIの依存。テストからは override して実サービスへ到達させない（INV-18）。"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.api.workflow_starter import TemporalWorkflowStarter, WorkflowStarter
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.artifact_store import ArtifactStore


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return session_factory_from_settings(get_settings())


async def get_workflow_starter() -> WorkflowStarter:  # pragma: no cover - 実接続経路
    return await TemporalWorkflowStarter.connect(get_settings())


def get_artifact_store_factory() -> Callable[[], ArtifactStore]:  # pragma: no cover - 実接続経路
    """ArtifactStore は必要になるまで作らない（pin 済み計画の有無は DB の行だけで先に分かる）。"""

    def make() -> ArtifactStore:
        from infrastructure.storage.minio_store import MinioArtifactStore

        return MinioArtifactStore.from_settings(get_settings())

    return make
