"""Upload worker wired to the in-memory ``FakeVideoUploader`` (smoke only, ADR-0020).

Used by ``scripts/smoke-upload.sh`` to exercise the real UploadWorkflow / activities against
real Temporal + PostgreSQL + MinIO **without contacting YouTube**. Lives under ``tests/`` so
production wiring (``workers.upload.run_worker``) can never import it, and refuses to start
unless ``AVP_FAKE_YOUTUBE=1``.

Logs ``videos_created`` / ``sessions_started`` after every upload activity (no session URIs).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from infrastructure.config import Settings
from infrastructure.db.session import session_factory_from_settings
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.connect import connect_with_retry
from infrastructure.temporal.run_inspector import TemporalWorkflowRunInspector
from infrastructure.workdir import WorkDirectory
from tests.support.fake_youtube import FakeVideoUploader
from workers.upload.activities import UploadActivities
from workers.upload.run_worker import build_workers, uploads_paused_switch

logger = logging.getLogger("fake_upload_worker")

FAKE_CHUNK_BYTES = 1024 * 1024
FAKE_CHANNEL_ID = "UC" + "f" * 22


class CountingUploader:
    """Delegates to the fake and logs counters once a video exists (never the session ref)."""

    def __init__(self, inner: FakeVideoUploader) -> None:
        self.inner = inner

    def _log(self, where: str) -> None:
        logger.info(
            "FAKE_YOUTUBE %s videos_created=%d sessions_started=%d chunk_sends=%d",
            where,
            self.inner.videos_created,
            self.inner.sessions_started,
            self.inner.chunk_sends,
        )

    async def start_session(self, metadata_json: Any, total_bytes: int, content_type: str):
        ref = await self.inner.start_session(metadata_json, total_bytes, content_type)
        self._log("start_session")
        return ref

    async def query_status(self, session: Any):
        return await self.inner.query_status(session)

    async def send_chunk(self, session: Any, offset: int, chunk: bytes, total_bytes: int):
        progress = await self.inner.send_chunk(session, offset, chunk, total_bytes)
        if offset + len(chunk) >= total_bytes:
            privacy = [
                (v.metadata.get("status") or {}).get("privacyStatus")
                for v in self.inner.videos.values()
            ]
            self._log(f"final_chunk privacy={privacy}")
        return progress

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        return await self.inner.find_video_by_marker(marker_tag)

    async def own_channel_id(self) -> str:
        return await self.inner.own_channel_id()

    async def processing_status(self, video_id: str):
        state = await self.inner.processing_status(video_id)
        logger.info(
            "FAKE_YOUTUBE processing_status upload_status=%s privacy=%s checks=%d",
            state.upload_status,
            state.privacy_status,
            self.inner.processing_checks,
        )
        return state


async def main() -> None:
    if os.environ.get("AVP_FAKE_YOUTUBE") != "1":
        raise SystemExit("fake upload worker: refusing to start without AVP_FAKE_YOUTUBE=1")
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    uploader = CountingUploader(
        FakeVideoUploader(chunk_bytes=FAKE_CHUNK_BYTES, channel_id=FAKE_CHANNEL_ID)
    )
    client = await connect_with_retry(settings)
    store = MinioArtifactStore.from_settings(settings)
    await store.ensure_bucket()
    session_factory = session_factory_from_settings(settings)
    activities = UploadActivities(
        session_factory=session_factory,
        store=store,
        bucket=settings.minio_bucket,
        workdir=WorkDirectory(settings.ai_video_work_root),
        uploader=uploader,
        channel_id=FAKE_CHANNEL_ID,
        chunk_bytes=FAKE_CHUNK_BYTES,
        uploads_paused=uploads_paused_switch(settings, session_factory),
        run_inspector=TemporalWorkflowRunInspector(client),
    )
    state, media = build_workers(client, activities)
    logger.info("FAKE_YOUTUBE upload worker listening (no network uploads)")
    async with state, media:
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
