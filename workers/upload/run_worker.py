"""Upload worker のエントリポイント（ADR-0020）。

1プロセスで2つの Worker: ``upload``（UploadWorkflow・状態系 Activity）と ``upload-media``
（投稿 Activity だけ。並行数 ``DEFAULT_UPLOAD_CONCURRENCY`` = 1）。
YouTube の設定・refresh token ファイルを**起動時に検証**し、欠けていれば Temporal に繋ぐ前に止まる。
secret の値は表示しない。
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import UPLOAD_MEDIA_TASK_QUEUE, UPLOAD_TASK_QUEUE
from contracts.upload import DEFAULT_UPLOAD_CONCURRENCY, YOUTUBE_CHANNEL_ID_PATTERN
from domain.upload.ports import VideoUploader
from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.run_inspector import TemporalWorkflowRunInspector
from infrastructure.workdir import WorkDirectory
from infrastructure.youtube.errors import YouTubeAuthError, YouTubeError
from infrastructure.youtube.oauth import RefreshTokenCredentials
from infrastructure.youtube.uploader import YouTubeResumableUploader, validate_chunk_bytes
from workers.upload.activities import UploadActivities
from workers.upload.workflows import UploadWorkflow

logger = logging.getLogger(__name__)

#: 送信・照会 1 回の HTTP timeout（秒）。8 MiB を低速回線でも送り切れる値
HTTP_TIMEOUT_SECONDS = 300.0


def require_channel_id(settings: Settings) -> str:
    channel = settings.youtube_channel_id or ""
    if not re.fullmatch(YOUTUBE_CHANNEL_ID_PATTERN, channel):
        raise SystemExit(
            "upload worker: YOUTUBE_CHANNEL_ID is missing or not a channel id (UC...); "
            "see docs/operations/upload-worker.md"
        )
    return channel


def build_uploader(settings: Settings, client: httpx.AsyncClient) -> YouTubeResumableUploader:
    """設定を検証して uploader を組む。欠け・不正は ``SystemExit``（値は表示しない）。"""
    missing = [
        name
        for name, value in (
            ("YOUTUBE_CLIENT_ID", settings.youtube_client_id),
            ("YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret),
            ("YOUTUBE_REFRESH_TOKEN_PATH", settings.youtube_refresh_token_path),
        )
        if not value
    ]
    if missing:
        raise SystemExit(
            f"upload worker: {', '.join(missing)} not set "
            "(run scripts/youtube-oauth.py; see docs/operations/upload-worker.md)"
        )
    try:
        chunk_bytes = validate_chunk_bytes(settings.youtube_chunk_bytes)
    except ValueError as exc:
        raise SystemExit(f"upload worker: YOUTUBE_CHUNK_BYTES: {exc}") from None
    assert settings.youtube_client_id and settings.youtube_client_secret
    assert settings.youtube_refresh_token_path
    try:
        credentials = RefreshTokenCredentials.from_file(
            settings.youtube_client_id,
            settings.youtube_client_secret,
            settings.youtube_refresh_token_path,
            client=client,
        )
    except YouTubeAuthError as exc:
        # メッセージは adapter が token を含めないよう作っている
        raise SystemExit(f"upload worker: {exc}") from None
    return YouTubeResumableUploader(credentials, client=client, chunk_bytes=chunk_bytes)


async def verify_channel(uploader: VideoUploader, channel_id: str) -> None:
    """認証中のチャンネルが ``YOUTUBE_CHANNEL_ID`` と違えば起動しない（誤チャンネル防止）。"""
    try:
        own = await uploader.own_channel_id()
    except YouTubeError as exc:
        raise SystemExit(f"upload worker: cannot read the authenticated channel: {exc}") from None
    if own != channel_id:
        raise SystemExit(
            "upload worker: the OAuth credentials belong to a different channel than "
            "YOUTUBE_CHANNEL_ID; re-run scripts/youtube-oauth.py with the right account"
        )


def build_workers(client: Client, activities: UploadActivities) -> tuple[Worker, Worker]:
    state = Worker(
        client,
        task_queue=UPLOAD_TASK_QUEUE,
        workflows=[UploadWorkflow],
        activities=activities.state_activities(),
    )
    media = Worker(
        client,
        task_queue=UPLOAD_MEDIA_TASK_QUEUE,
        activities=activities.media_activities(),
        max_concurrent_activities=DEFAULT_UPLOAD_CONCURRENCY,
    )
    return state, media


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx は URL（session URI を含む）を INFO で出すので抑える（INV-20）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    settings = Settings()
    channel_id = require_channel_id(settings)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as http:
        uploader = build_uploader(settings, http)
        await verify_channel(uploader, channel_id)
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        store = MinioArtifactStore.from_settings(settings)
        await store.ensure_bucket()
        activities = UploadActivities(
            session_factory=session_factory_from_settings(settings),
            store=store,
            bucket=settings.minio_bucket,
            workdir=WorkDirectory(settings.ai_video_work_root),
            uploader=uploader,
            channel_id=channel_id,
            uploads_paused=lambda: settings.uploads_paused,
            chunk_bytes=uploader.chunk_bytes,
            run_inspector=TemporalWorkflowRunInspector(client),
        )
        state, media = build_workers(client, activities)
        logger.info(
            "upload worker listening on %s (workflow/state) and %s (upload x%s); paused=%s",
            UPLOAD_TASK_QUEUE,
            UPLOAD_MEDIA_TASK_QUEUE,
            DEFAULT_UPLOAD_CONCURRENCY,
            settings.uploads_paused,
        )
        async with state, media:
            await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
