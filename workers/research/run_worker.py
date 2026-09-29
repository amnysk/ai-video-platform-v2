"""Research worker のエントリポイント（ADR-0037 §8.5）。

compose service ``research-worker`` として常駐させる。queue ``research``
（``contracts.research.RESEARCH_TASK_QUEUE``）に ``ResearchWorkflow`` と research の Activity を
登録する。

- Provider は ``RESEARCH_PROVIDER``（``none`` = 既定。依頼は外部を呼ばずに ``blocked`` / ``fake`` =
  固定コーパス。実ネットワークに出ない）。実 Provider は registry に無い（ADR-0037 §6）
- 環境は DB・MinIO・Temporal と ``RESEARCH_PROVIDER`` だけ。``YOUTUBE_*`` / ``CODEX_*`` /
  ``FAL_KEY`` を持たない（``tests/contract/test_research_worker_compose.py``）
- Episode の工程の workflow・Activity は登録しない（INV-37）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.research import RESEARCH_TASK_QUEUE
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.research.executor import ResearchExecutor
from infrastructure.research.registry import (
    build_cost_model,
    build_handlers,
    build_providers,
)
from infrastructure.storage.artifact_store import ArtifactStore
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.research.activities import ResearchActivities
from workers.research.workflows import ResearchWorkflow

logger = logging.getLogger(__name__)


def build_activities(
    settings: Settings,
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: ArtifactStore,
    bucket: str | None = None,
) -> ResearchActivities:
    """設定から Provider・Handler・見積もりを組み、実行器を包む。

    組み立ての唯一の場所は registry（ここは呼ぶだけ）。
    """
    executor = ResearchExecutor(
        session_factory=session_factory,
        store=store,
        bucket=bucket or settings.minio_bucket,
        providers=build_providers(settings),
        handlers=build_handlers(settings),
        cost_model=build_cost_model(settings),
    )
    return ResearchActivities(executor)


def build_worker(
    client: Client,
    activities: ResearchActivities,
    *,
    task_queue: str = RESEARCH_TASK_QUEUE,
    activities_override: Sequence[Callable[..., object]] | None = None,
) -> Worker:
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ResearchWorkflow],
        activities=list(activities_override or activities.all_activities()),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    activities = build_activities(
        settings,
        session_factory=session_factory_from_settings(settings),
        store=store,
    )
    logger.info(
        "research worker listening on task queue %s (provider=%s)",
        RESEARCH_TASK_QUEUE,
        settings.research_provider,
    )
    async with build_worker(client, activities):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
