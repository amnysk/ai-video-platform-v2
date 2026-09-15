"""UploadWorkflow + 本物の UploadActivities で YouTube の処理状態を待つ（ADR-0022）。

time-skipping サーバ・SQLite・メモリストア・FakeVideoUploader。どの経路でも動画は1本（INV-14）。
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.states import EpisodeStatus, FailureClass
from contracts.upload_activities import (
    UploadAdmitRequest,
    UploadFinalVideoRequest,
    UploadRecordFailureRequest,
)
from infrastructure.db.models import Base
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.storage.memory_store import InMemoryArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.fake_youtube import FakeVideoUploader
from tests.support.upload import CHANNEL_ID, TEST_CHUNK_BYTES, seed_render_ready
from workers.upload.activities import UploadActivities
from workers.upload.workflows import UploadWorkflow, UploadWorkflowInput


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'proc.db'}",
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


class Stage:
    def __init__(self, env: WorkflowEnvironment, factory, tmp_path: Path) -> None:
        self.env = env
        self.factory = factory
        self.tmp_path = tmp_path
        self.store = InMemoryArtifactStore()
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
        self.episode_id = ""
        self.queue = f"upload-proc-{uuid.uuid4().hex[:8]}"

    @property
    def workflow_id(self) -> str:
        return f"episode-{self.episode_id}-upload"

    def acts(self) -> UploadActivities:
        return UploadActivities(
            session_factory=self.factory,
            store=self.store,
            bucket="artifacts",
            workdir=WorkDirectory(self.tmp_path / "work", forbidden=()),
            uploader=self.fake,
            channel_id=CHANNEL_ID,
            chunk_bytes=TEST_CHUNK_BYTES,
            transient_backoff_seconds=0.0,
            expiry_confirm_delay_seconds=0.0,
            heartbeat=lambda *_: None,
        )

    @asynccontextmanager
    async def workers(self):
        acts = self.acts()
        async with (
            Worker(
                self.env.client,
                task_queue=self.queue,
                workflows=[UploadWorkflow],
                activities=acts.state_activities(),
            ),
            Worker(
                self.env.client,
                task_queue=f"{self.queue}-media",
                activities=acts.media_activities(),
                max_concurrent_activities=1,
            ),
        ):
            yield

    async def run(self):
        handle = await self.env.client.start_workflow(
            UploadWorkflow.run,
            UploadWorkflowInput(
                episode_id=self.episode_id, upload_task_queue=f"{self.queue}-media"
            ),
            id=self.workflow_id,
            task_queue=self.queue,
        )
        return await asyncio.wait_for(handle.result(), timeout=120)

    async def status(self) -> EpisodeStatus:
        async with self.factory() as session:
            episode = await EpisodeRepository(session).get(self.episode_id)
        assert episode is not None
        return episode.status


@pytest_asyncio.fixture
async def stage(env, factory, tmp_path) -> Stage:
    s = Stage(env, factory, tmp_path)
    s.episode_id = await seed_render_ready(factory, s.store, tmp_path)
    return s


async def test_processed_video_becomes_uploaded(stage: Stage) -> None:
    async with stage.workers():
        result = await stage.run()
    assert result.status == EpisodeStatus.UPLOADED.value
    assert result.processing is not None and result.processing.reason == "processed"
    assert stage.fake.videos_created == 1 and stage.fake.processing_checks == 1


async def test_pending_then_processed_becomes_uploaded_without_reupload(stage: Stage) -> None:
    fake = stage.fake
    fake.script_processing(
        fake.processing_state(found=False, upload_status=None, processing_status=None),
        fake.processing_state(upload_status="uploaded", processing_status="processing"),
        fake.processing_state(),
    )
    async with stage.workers():
        result = await stage.run()
    assert result.status == EpisodeStatus.UPLOADED.value
    assert fake.processing_checks == 3
    assert fake.videos_created == 1 and fake.sessions_started == 1


async def test_rejected_blocks_and_repost_checks_again_without_reupload(stage: Stage) -> None:
    fake = stage.fake
    fake.script_processing(
        fake.processing_state(upload_status="rejected", rejection_reason="claim")
    )
    async with stage.workers():
        first = await stage.run()
        assert first.status == EpisodeStatus.BLOCKED.value
        assert first.failure_class == FailureClass.NEEDS_INPUT.value
        assert await stage.status() is EpisodeStatus.BLOCKED

        again = await stage.run()  # POST での再開
    assert again.admitted and again.status == EpisodeStatus.BLOCKED.value
    assert fake.videos_created == 1 and fake.sessions_started == 1
    assert fake.processing_checks == 2


async def test_channel_mismatch_blocks(stage: Stage) -> None:
    stage.fake.script_processing(stage.fake.processing_state(channel_id="UC" + "z" * 22))
    async with stage.workers():
        result = await stage.run()
    assert result.status == EpisodeStatus.BLOCKED.value
    assert stage.fake.videos_created == 1


async def test_not_private_blocks(stage: Stage) -> None:
    stage.fake.script_processing(stage.fake.processing_state(privacy_status="public"))
    async with stage.workers():
        result = await stage.run()
    assert result.status == EpisodeStatus.BLOCKED.value
    assert stage.fake.videos_created == 1


async def test_interrupted_after_upload_before_processing_resumes_without_duplicate(
    stage: Stage,
) -> None:
    """投稿は完了したが処理確認の前に止まった（worker 停止 → cancel で blocked）。

    再開は再投稿しない。
    """
    acts = stage.acts()
    admitted = await acts.admit(UploadAdmitRequest(stage.episode_id, stage.workflow_id, "run-0"))
    assert admitted.admitted
    uploaded = await acts.upload_final_video(
        UploadFinalVideoRequest(stage.episode_id, stage.workflow_id, "run-0")
    )
    await acts.record_failure(
        UploadRecordFailureRequest(
            stage.episode_id, stage.workflow_id, "run-0", "needs_input", "worker stopped", False
        )
    )
    assert stage.fake.videos_created == 1 and stage.fake.processing_checks == 0

    async with stage.workers():
        result = await stage.run()

    assert result.status == EpisodeStatus.UPLOADED.value
    assert result.upload is not None and result.upload.skipped
    assert result.upload.video_id == uploaded.video_id
    assert stage.fake.videos_created == 1 and stage.fake.sessions_started == 1
    assert stage.fake.processing_checks == 1
