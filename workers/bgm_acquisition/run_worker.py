"""BGM 収集 worker のエントリポイント（ADR-0087 §3 / §10）。

compose service ``bgm-acquisition-worker`` として動かす。task queue ``bgm-acquisition``。

- ``BGM_ACQUISITION_ENABLED`` **と** ``BGM_ENABLED`` が両方 true でなければ
  **起動しない**（``SystemExit``）。
  OFF の間はこの worker がネットワークに触れない（INV-159）
- Wikimedia の User-Agent には連絡先が要る（``BGM_ACQUISITION_CONTACT``。メールまたは URL）
- Episode の工程・render・upload の workflow / Activity は登録しない
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.music_sources import BGM_ACQUISITION_TASK_QUEUE
from domain.music.ports import BgmCatalogRepository
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.music.acquisition import AcquisitionService
from infrastructure.music.audio_inspect import FfmpegAudioInspector
from infrastructure.music.sources.commons import CommonsClient
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from workers.bgm_acquisition.activities import BgmAcquisitionActivities
from workers.bgm_acquisition.workflows import BgmAcquisitionWorkflow

logger = logging.getLogger(__name__)

CONTACT_ENV = "BGM_ACQUISITION_CONTACT"
USER_AGENT_TEMPLATE = "avp2-bgm-acquisition/1.0 ({contact}) httpx"


def require_enabled(settings: Settings) -> None:
    """起動の前提。満たさなければ ``SystemExit``（Temporal にも DB にも繋ぐ前）。"""
    if not settings.bgm_acquisition_enabled:
        raise SystemExit(
            "bgm acquisition worker: BGM_ACQUISITION_ENABLED is not true; refusing to start "
            "(this worker is the only one that touches the network for music)"
        )
    if not settings.bgm_enabled:
        raise SystemExit(
            "bgm acquisition worker: BGM_ACQUISITION_ENABLED requires BGM_ENABLED=true "
            "(ADR-0087 §10)"
        )


def build_user_agent(contact: str | None) -> str:
    value = (contact or "").strip()
    if not value:
        raise SystemExit(
            f"bgm acquisition worker: {CONTACT_ENV} (email or URL) is required for the "
            "Wikimedia User-Agent policy"
        )
    return USER_AGENT_TEMPLATE.format(contact=value)


def build_repository(session_factory: async_sessionmaker[AsyncSession]) -> BgmCatalogRepository:
    """PostgreSQL 版 catalog（``infrastructure/db/bgm_repositories.py``）。"""
    module = importlib.import_module("infrastructure.db.bgm_repositories")
    return module.SqlBgmCatalogRepository(session_factory)  # type: ignore[no-any-return]


def build_worker(client: Client, activities: BgmAcquisitionActivities) -> Worker:
    return Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=BGM_ACQUISITION_TASK_QUEUE,
        workflows=[BgmAcquisitionWorkflow],
        activities=list(activities.all_activities()),
        max_concurrent_activities=1,  # 直列（Wikimedia の方針）
    )


async def main() -> None:
    settings = Settings()
    require_enabled(settings)
    user_agent = build_user_agent(os.environ.get(CONTACT_ENV))
    if not settings.render_ffmpeg_path:
        raise SystemExit(
            "bgm acquisition worker: RENDER_FFMPEG_PATH is required for audio inspection"
        )
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    repo = build_repository(session_factory_from_settings(settings))
    source = CommonsClient(user_agent=user_agent)
    service = AcquisitionService(
        source=source,
        repo=repo,
        store=store,
        inspector=FfmpegAudioInspector(
            settings.render_ffmpeg_path, ffprobe_path=settings.render_ffprobe_path
        ),
        clock=lambda: datetime.now(UTC),
    )
    worker = build_worker(client, BgmAcquisitionActivities(repo=repo, service=service))
    logger.info("bgm acquisition worker listening on task queue %s", BGM_ACQUISITION_TASK_QUEUE)
    try:
        async with worker:
            await asyncio.Event().wait()
    finally:
        await source.aclose()


if __name__ == "__main__":
    asyncio.run(main())
