"""Render worker のエントリポイント（ADR-0019）。

task queue ``render`` に RenderWorkflow・状態系 Activity・描画 Activity を登録する。
固定版 ffmpeg の sha256 を**起動時に検証**し、合わなければ Temporal に繋ぐ前に止まる。
"""

from __future__ import annotations

import asyncio
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import RENDER_TASK_QUEUE
from domain.errors import RenderEngineUnavailableError
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from workers.render.activities import RenderActivities
from workers.render.run_inspector import TemporalWorkflowRunInspector
from workers.render.workflows import RenderWorkflow

logger = logging.getLogger(__name__)


def build_engine(settings: Settings) -> FfmpegRenderEngine:
    """設定の ffmpeg を検証して組む。無い・不一致なら ``SystemExit``（fail fast）。"""
    if not settings.render_ffmpeg_path or not settings.render_ffmpeg_sha256:
        raise SystemExit(
            "render worker: RENDER_FFMPEG_PATH / RENDER_FFMPEG_SHA256 are not set "
            "(run scripts/install-render-ffmpeg.sh; see docs/operations/render-worker.md)"
        )
    try:
        engine = FfmpegRenderEngine.from_binary(
            settings.render_ffmpeg_path,
            settings.render_ffmpeg_sha256,
            threads=settings.render_ffmpeg_threads,
        )
    except RenderEngineUnavailableError as exc:
        logger.error("render worker: ffmpeg verification failed: %s", exc)
        raise SystemExit(f"render worker: {exc}") from exc
    identity = engine.identity()
    logger.info(
        "render engine verified: %s %s sha256=%s",
        identity.engine,
        identity.version,
        identity.binary_sha256,
    )
    return engine


def build_worker(client: Client, settings: Settings, activities: RenderActivities) -> Worker:
    return Worker(
        client,
        task_queue=RENDER_TASK_QUEUE,
        workflows=[RenderWorkflow],
        activities=activities.all_activities(),
        max_concurrent_activities=max(1, settings.render_concurrency),
    )


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    engine = build_engine(settings)
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    activities = RenderActivities(
        session_factory=session_factory_from_settings(settings),
        store=store,
        bucket=settings.minio_bucket,
        workdir=WorkDirectory(settings.ai_video_work_root),
        engine=engine,
        probe=PillowAvMediaProbe(),
        font_path=settings.render_font_path,
        font_sha256=settings.render_font_sha256,
        render_timeout_seconds=settings.render_timeout_seconds,
        min_free_bytes=settings.render_min_free_bytes,
        run_inspector=TemporalWorkflowRunInspector(client),
    )
    logger.info("render worker listening on task queue %s", RENDER_TASK_QUEUE)
    async with build_worker(client, settings, activities):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
