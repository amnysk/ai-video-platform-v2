"""子プロセス: 本物の UploadWorkflow + 本物の UploadActivities（実 PostgreSQL 一時スキーマ・
実 MinIO）と、ファイルに状態を残す fake uploader（YouTube に出ない）。

queue は ``DURABILITY_QUEUE``（workflow/状態）と ``<queue>-media``（投稿）。どちらも一意。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from temporalio.client import Client
from temporalio.worker import Worker

from infrastructure.config import Settings
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.temporal.run_inspector import TemporalWorkflowRunInspector
from infrastructure.workdir import WorkDirectory
from tests.support.durability.common import (
    ENV_CHUNK_DELAY,
    ENV_DB_URL,
    ENV_QUEUE,
    ENV_SCHEMA,
    ENV_STATE_FILE,
    ENV_WORK_DIR,
    mark_ready,
    schema_session_factory,
)
from tests.support.durability.file_uploader import FileBackedFakeUploader
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES
from workers.upload.activities import UploadActivities
from workers.upload.workflows import UploadWorkflow


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    queue = os.environ[ENV_QUEUE]
    assert queue not in {"upload", "upload-media"}
    factory = schema_session_factory(os.environ[ENV_DB_URL], os.environ[ENV_SCHEMA])
    client = await Client.connect(os.environ["TEMPORAL_ADDRESS"], namespace="default")
    uploader = FileBackedFakeUploader(
        Path(os.environ[ENV_STATE_FILE]),
        chunk_delay=float(os.environ.get(ENV_CHUNK_DELAY, "0")),
        chunk_bytes=TEST_CHUNK_BYTES,
        channel_id=CHANNEL_ID,
    )
    activities = UploadActivities(
        session_factory=factory,
        store=MinioArtifactStore.from_settings(Settings()),
        bucket="artifacts",
        workdir=WorkDirectory(Path(os.environ[ENV_WORK_DIR]), forbidden=()),
        uploader=uploader,
        channel_id=CHANNEL_ID,
        chunk_bytes=TEST_CHUNK_BYTES,
        marker_lookup_attempts=2,
        marker_lookup_delay_seconds=0.0,
        transient_backoff_seconds=0.0,
        expiry_confirm_delay_seconds=0.0,
        run_inspector=TemporalWorkflowRunInspector(client),
    )
    state = Worker(
        client,
        task_queue=queue,
        workflows=[UploadWorkflow],
        activities=activities.state_activities(),
    )
    media = Worker(
        client,
        task_queue=f"{queue}-media",
        activities=activities.media_activities(),
        max_concurrent_activities=1,
    )
    async with state, media:
        mark_ready()
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
