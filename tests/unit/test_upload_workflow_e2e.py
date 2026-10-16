"""UploadWorkflow + 本物の UploadActivities（SQLite・メモリストア・FakeVideoUploader）を
time-skipping サーバで通す（ADR-0020）。二重起動・再実行でも動画は1本（INV-14）。
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.states import EpisodeStatus
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
        f"sqlite+aiosqlite:///{tmp_path / 'wf.db'}",
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


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    assert episode is not None
    return episode.status


async def test_double_start_is_refused_and_upload_happens_once(env, factory, tmp_path) -> None:
    store = InMemoryArtifactStore()
    episode_id = await seed_render_ready(factory, store, tmp_path)
    fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
    acts = UploadActivities(
        session_factory=factory,
        store=store,
        bucket="artifacts",
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
        uploader=fake,
        channel_id=CHANNEL_ID,
        chunk_bytes=TEST_CHUNK_BYTES,
        transient_backoff_seconds=0.0,
        expiry_confirm_delay_seconds=0.0,
    )
    queue = f"upload-e2e-{uuid.uuid4().hex[:8]}"
    async with (
        Worker(
            env.client,
            task_queue=queue,
            workflows=[UploadWorkflow],
            activities=acts.state_activities(),
        ),
        Worker(
            env.client,
            task_queue=f"{queue}-media",
            activities=acts.media_activities(),
            max_concurrent_activities=1,
        ),
    ):
        workflow_id = f"episode-{episode_id}-upload"

        def start():
            return env.client.start_workflow(
                UploadWorkflow.run,
                UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{queue}-media"),
                id=workflow_id,
                task_queue=queue,
            )

        # 同じ Episode への二重 POST: 同じ workflow id の2つ目は Temporal が拒否する
        handle = await start()
        with pytest.raises(WorkflowAlreadyStartedError):
            await start()
        first = await asyncio.wait_for(handle.result(), timeout=60)
        assert first.admitted and first.status == EpisodeStatus.UPLOADED.value
        assert fake.videos_created == 1
        assert await _status(factory, episode_id) is EpisodeStatus.UPLOADED

        # uploaded からの再実行は admit で止まり、動画は増えない
        again = await asyncio.wait_for((await start()).result(), timeout=60)
        assert not again.admitted and again.status == EpisodeStatus.UPLOADED.value
        assert fake.videos_created == 1 and fake.sessions_started == 1
