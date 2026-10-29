"""Render worker のエントリポイント（ADR-0019）。

1プロセスで2つの Worker を動かす: ``render``（RenderWorkflow・状態系 Activity）と
``render-media``（描画 Activity。並行数 = render_concurrency）。
固定版 ffmpeg の sha256 を**起動時に検証**し、合わなければ Temporal に繋ぐ前に止まる。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import RENDER_MEDIA_TASK_QUEUE, RENDER_TASK_QUEUE
from contracts.upload import description_composition_enabled
from domain.errors import RenderEngineUnavailableError
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.geo.clip import FfmpegMapClipEncoder
from infrastructure.geo.guard import require_quality_gate
from infrastructure.geo.service import GeoRenderService
from infrastructure.geo.source import LocalDirectoryGeoSource
from infrastructure.logging.temporal import worker_interceptors
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.music.guard import require_description_composition_for_bgm
from infrastructure.quality.wiring import build_quality_record_writer
from infrastructure.render.bgm_intelligibility import FfmpegSpeechMarginProbe
from infrastructure.render.bgm_source import LocalDirectoryBgmSource
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from infrastructure.render.loudness import FfmpegLoudnessProbe
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from infrastructure.workdir import WorkDirectory
from workers.render.activities import RenderActivities
from workers.render.run_inspector import TemporalWorkflowRunInspector
from workers.render.workflows import RenderWorkflow

logger = logging.getLogger(__name__)

#: 停止時（docker compose stop / SIGTERM）に実行中の Activity の完了を待つ上限。
#: compose の stop_grace_period（120s）より短くする（ADR-0024）。
GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=100)


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


def build_workers(
    client: Client, settings: Settings, activities: RenderActivities
) -> tuple[Worker, Worker]:
    """workflow + 状態系（通常の並行枠）と、描画だけ（並行数 = render_concurrency）の2つ。

    同じ枠を共有すると、長い描画の間に他 Episode の admit / record_failure が待たされる。
    """
    state = Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=RENDER_TASK_QUEUE,
        workflows=[RenderWorkflow],
        activities=activities.state_activities(),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    media = Worker(
        client,
        interceptors=worker_interceptors(),
        task_queue=RENDER_MEDIA_TASK_QUEUE,
        activities=activities.media_activities(),
        max_concurrent_activities=max(1, settings.render_concurrency),
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
    )
    return state, media


async def main() -> None:
    settings = Settings()
    engine = build_engine(settings)
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    # ADR-0077: 設定 ON のときだけ供給元と測定を渡す。OFF は導入前と同一
    require_description_composition_for_bgm(
        bgm_on=settings.bgm_enabled, composition_enabled=description_composition_enabled()
    )
    bgm_source = None
    loudness_probe = None
    speech_probe = None
    if settings.bgm_enabled and settings.render_ffmpeg_path:
        bgm_source = LocalDirectoryBgmSource(Path(settings.ai_video_work_root) / "bgm")
        loudness_probe = FfmpegLoudnessProbe(settings.render_ffmpeg_path)
        # ADR-0086 §3: 発話中の声と BGM の差を描画の中で実測する（BGM_ENABLED の背後）
        speech_probe = FfmpegSpeechMarginProbe(settings.render_ffmpeg_path)
    # ADR-0084: 設定 ON のときだけ地図の結線を渡す。OFF は導入前と同一
    geo = None
    if settings.geo_maps_enabled:
        require_quality_gate(settings)
        if not settings.render_ffmpeg_path:
            raise SystemExit("render worker: GEO_MAPS_ENABLED needs RENDER_FFMPEG_PATH")
        geo = GeoRenderService(
            source=LocalDirectoryGeoSource(Path(settings.ai_video_work_root) / "geo"),
            encoder=FfmpegMapClipEncoder(settings.render_ffmpeg_path),
            font_path=Path(settings.render_font_path),
        )
    session_factory = session_factory_from_settings(settings)
    activities = RenderActivities(
        session_factory=session_factory,
        store=store,
        bucket=settings.minio_bucket,
        workdir=WorkDirectory(settings.ai_video_work_root),
        engine=engine,
        probe=PillowAvMediaProbe(),
        source_probe=PillowAvMediaProbe(),
        render_threads=settings.render_ffmpeg_threads,
        font_path=settings.render_font_path,
        font_sha256=settings.render_font_sha256,
        render_timeout_seconds=settings.render_timeout_seconds,
        min_free_bytes=settings.render_min_free_bytes,
        run_inspector=TemporalWorkflowRunInspector(client),
        bgm_source=bgm_source,
        loudness_probe=loudness_probe,
        geo=geo,
        speech_probe=speech_probe,
        # ADR-0086 §2: AVP_QUALITY_GATE_ENFORCE が ON のときだけ使用宣言を書く（OFF は None）
        quality_writer=build_quality_record_writer(settings, session_factory),
    )
    state, media = build_workers(client, settings, activities)
    logger.info(
        "render worker listening on task queues %s (workflow/state) and %s (render x%s)",
        RENDER_TASK_QUEUE,
        RENDER_MEDIA_TASK_QUEUE,
        max(1, settings.render_concurrency),
    )
    async with state, media:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
