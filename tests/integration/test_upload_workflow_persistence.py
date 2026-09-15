"""実 PostgreSQL（一時スキーマ）+ 実 MinIO + 実 Temporal + fake uploader（ADR-0020）。

YouTube だけ fake。台帳・受領・状態遷移・版管理は本物。**共有 DB を壊さない**（一時スキーマ）。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from contracts.upload import parse_upload_receipt
from contracts.upload_activities import UploadAdmitRequest, UploadFinalVideoRequest
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservation,
)
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.fake_youtube import FAKE_SESSION_PREFIX, FakeVideoUploader
from tests.support.upload import (
    CHANNEL_ID,
    TEST_CHUNK_BYTES,
    Crash,
    CrashingUploader,
    seed_render_ready,
)
from workers.upload.activities import UploadActivities
from workers.upload.workflows import UploadWorkflow, UploadWorkflowInput

TEST_DATABASE_URL = require_test_database_url()
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not TEMPORAL_ADDRESS,
    reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS are required",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"upload_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from infrastructure.config import Settings

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    return store


class Stack:
    def __init__(self, factory, store, client: Client, tmp_path) -> None:
        self.factory = factory
        self.store = store
        self.client = client
        self.tmp_path = tmp_path
        self.fake = FakeVideoUploader(chunk_bytes=TEST_CHUNK_BYTES)
        self.queue = f"upload-it-{uuid.uuid4().hex[:10]}"
        self.activities = self.make()

    def make(self, uploader=None, **overrides) -> UploadActivities:
        kwargs = {
            "session_factory": self.factory,
            "store": self.store,
            "bucket": "artifacts",
            "workdir": WorkDirectory(self.tmp_path / "work", forbidden=()),
            "uploader": uploader or self.fake,
            "channel_id": CHANNEL_ID,
            "chunk_bytes": TEST_CHUNK_BYTES,
            "marker_lookup_attempts": 2,
            "marker_lookup_delay_seconds": 0.0,
            "transient_backoff_seconds": 0.0,
            "expiry_confirm_delay_seconds": 0.0,
        }
        kwargs.update(overrides)
        return UploadActivities(**kwargs)

    def workers(self):
        return (
            Worker(
                self.client,
                task_queue=self.queue,
                workflows=[UploadWorkflow],
                activities=self.activities.state_activities(),
            ),
            Worker(
                self.client,
                task_queue=f"{self.queue}-media",
                activities=self.activities.media_activities(),
                max_concurrent_activities=1,
            ),
        )

    async def start(self, episode_id: str):
        return await self.client.start_workflow(
            UploadWorkflow.run,
            UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{self.queue}-media"),
            id=f"episode-{episode_id}-upload",
            task_queue=self.queue,
        )

    async def run(self, episode_id: str):
        state, media = self.workers()
        async with state, media:
            handle = await self.start(episode_id)
            return await asyncio.wait_for(handle.result(), timeout=90)

    async def reservation(self, episode_id: str) -> ProviderReservation:
        from sqlalchemy import select

        from infrastructure.db.models import ProviderReservationRow
        from infrastructure.db.repositories import _to_reservation

        async with self.factory() as session:
            rows = (
                await session.scalars(
                    select(ProviderReservationRow).where(
                        ProviderReservationRow.provider == ProviderCall.YOUTUBE_UPLOAD.value
                    )
                )
            ).all()
        (row,) = [r for r in rows if str(r.episode_id) == episode_id]
        return _to_reservation(row)

    async def status(self, episode_id: str) -> EpisodeStatus:
        async with self.factory() as session:
            episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status


@pytest_asyncio.fixture
async def stack(pg_session_factory, minio_store, tmp_path) -> Stack:
    client = await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")
    return Stack(pg_session_factory, minio_store, client, tmp_path)


async def test_happy_path_persists_spent_reservation_receipt_and_uploaded(stack: Stack) -> None:
    episode_id = await seed_render_ready(stack.factory, stack.store, stack.tmp_path)

    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.UPLOADED.value and result.upload is not None
    assert stack.fake.videos_created == 1
    assert await stack.status(episode_id) is EpisodeStatus.UPLOADED
    reservation = await stack.reservation(episode_id)
    assert reservation.status is ReservationStatus.SPENT
    assert reservation.provider_result_ref == result.upload.video_id
    assert reservation.outcome_artifact_id == result.upload.artifact_id
    assert reservation.dispatched_at is not None and reservation.reconciled_at is not None
    async with stack.factory() as session:
        meta = await ArtifactMetadataRepository(session).get(result.upload.artifact_id)
    assert meta is not None and meta.artifact_type is ArtifactType.UPLOAD_RECEIPT
    payload = await stack.store.get_json(meta.object_key)
    assert sha256_hex(canonical_json_bytes(payload)) == meta.sha256
    receipt = parse_upload_receipt(payload)
    assert receipt.video_id == result.upload.video_id and receipt.privacy_status == "private"
    assert FAKE_SESSION_PREFIX not in canonical_json_bytes(payload).decode()

    # uploaded への再実行は何もしない
    again = await stack.run(episode_id)
    assert not again.admitted and stack.fake.videos_created == 1


async def test_concurrent_double_start_and_concurrent_activities_make_one_video(
    stack: Stack,
) -> None:
    episode_id = await seed_render_ready(stack.factory, stack.store, stack.tmp_path)
    state, media = stack.workers()
    async with state, media:
        handle = await stack.start(episode_id)
        with pytest.raises(WorkflowAlreadyStartedError):
            await stack.start(episode_id)
        result = await asyncio.wait_for(handle.result(), timeout=90)
    assert result.status == EpisodeStatus.UPLOADED.value
    assert stack.fake.videos_created == 1

    # 活動レベル: 同じ upload key の2つの試行が PostgreSQL 上で競っても1本
    other = await seed_render_ready(stack.factory, stack.store, stack.tmp_path)
    acts = stack.make()
    admitted = await acts.admit(UploadAdmitRequest(other, "wf-x", "run-1"))
    assert admitted.admitted
    request = UploadFinalVideoRequest(other, "wf-x", "run-1")
    results = await asyncio.gather(
        stack.make().upload_final_video(request),
        stack.make().upload_final_video(request),
        return_exceptions=True,
    )
    ids = {r.video_id for r in results if not isinstance(r, BaseException)}
    assert len(ids) == 1, results
    assert stack.fake.videos_created == 2  # 1本目の Episode + この Episode の1本


async def test_crash_after_session_saved_resumes_to_one_video(stack: Stack) -> None:
    episode_id = await seed_render_ready(stack.factory, stack.store, stack.tmp_path)
    acts = stack.make(CrashingUploader(stack.fake, crash_on="first_send"))
    assert (await acts.admit(UploadAdmitRequest(episode_id, "wf-crash", "run-1"))).admitted
    with pytest.raises(Crash):
        await acts.upload_final_video(UploadFinalVideoRequest(episode_id, "wf-crash", "run-1"))
    saved = await stack.reservation(episode_id)
    assert saved.provider_job_ref and saved.dispatched_at is not None
    assert saved.status is ReservationStatus.RESERVED

    # 運用者の cancel 相当で blocked にし、upload workflow から再開する
    async with stack.factory() as session:
        episodes = EpisodeRepository(session)
        from domain.episode.transitions import EpisodeEvent

        await episodes.apply_event(episode_id, EpisodeEvent.NEEDS_INPUT_FAILURE)
        await episodes.set_workflow_id(episode_id, f"episode-{episode_id}-upload:old-run")
        await session.commit()
    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.UPLOADED.value
    assert stack.fake.videos_created == 1 and stack.fake.sessions_started == 1


async def test_unknown_outcome_blocks_and_keeps_the_single_session(stack: Stack) -> None:
    episode_id = await seed_render_ready(stack.factory, stack.store, stack.tmp_path)
    stack.fake.expire_session_at_offset = 2 * TEST_CHUNK_BYTES

    result = await stack.run(episode_id)

    assert result.status == EpisodeStatus.BLOCKED.value
    assert result.failure_class == "needs_input"
    assert stack.fake.videos_created == 0 and stack.fake.sessions_started == 1
    reservation = await stack.reservation(episode_id)
    assert reservation.status is ReservationStatus.RESERVED
    assert reservation.dispatched_at is not None and reservation.provider_result_ref is None
    from infrastructure.db.models import EpisodeRow

    async with stack.factory() as session:
        row = await session.get(EpisodeRow, uuid.UUID(episode_id))
    assert row is not None and row.blocked_reason
    assert FAKE_SESSION_PREFIX not in row.blocked_reason

    # 再 POST（blocked → 再開）でも新しい session を開かない
    stack.fake.expire_session_at_offset = None
    again = await stack.run(episode_id)
    assert again.status == EpisodeStatus.BLOCKED.value
    assert stack.fake.sessions_started == 1 and stack.fake.videos_created == 0
