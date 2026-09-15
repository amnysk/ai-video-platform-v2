"""実 PostgreSQL（一時スキーマ）+ 実 MinIO + 実 Temporal で render 工程を通す（ADR-0019）。

描画エンジンと probe だけ fake。計画・技術検査・版管理・読み戻し・状態遷移は本物。
**共有 DB を壊さない**: 一時スキーマに閉じ込め、最後にスキーマごと消す。
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
from temporalio.worker import Worker

from contracts.artifacts import parse_final_video
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.storage.minio_store import MinioArtifactStore
from infrastructure.workdir import WorkDirectory
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.render_activity import PassingSourceProbe, seed_render_inputs
from workers.render.activities import RenderActivities
from workers.render.run_inspector import TemporalWorkflowRunInspector
from workers.render.workflows import RenderWorkflow, RenderWorkflowInput

TEST_DATABASE_URL = require_test_database_url()
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not TEMPORAL_ADDRESS,
    reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS are required",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"render_test_{uuid.uuid4().hex[:12]}"
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
        font = tmp_path / "font.ttc"
        font.write_bytes(b"integration font")
        self.factory = factory
        self.store = store
        self.client = client
        self.probe = FakeFinalVideoProbe()
        self.engine = FakeRenderEngine(on_render=lambda r: setattr(self.probe, "plan", r.plan))
        self.queue = f"render-it-{uuid.uuid4().hex[:10]}"
        self.activities = RenderActivities(
            session_factory=factory,
            store=store,
            bucket="artifacts",
            workdir=WorkDirectory(tmp_path / "work", forbidden=()),
            engine=self.engine,
            probe=self.probe,
            source_probe=PassingSourceProbe(),
            font_path=font,
            font_sha256=sha256_hex(b"integration font"),
            render_timeout_seconds=60,
            min_free_bytes=0,
            run_inspector=TemporalWorkflowRunInspector(client),
        )

    async def run(self, episode_id: str, profile: str = "shorts_vertical"):
        async with (
            Worker(
                self.client,
                task_queue=self.queue,
                workflows=[RenderWorkflow],
                activities=self.activities.state_activities(),
            ),
            Worker(
                self.client,
                task_queue=f"{self.queue}-media",
                activities=self.activities.media_activities(),
                max_concurrent_activities=1,
            ),
        ):
            return await asyncio.wait_for(
                self.client.execute_workflow(
                    RenderWorkflow.run,
                    RenderWorkflowInput(
                        episode_id=episode_id,
                        render_profile_id=profile,
                        render_task_queue=f"{self.queue}-media",
                    ),
                    id=f"episode-{episode_id}-render",
                    task_queue=self.queue,
                ),
                timeout=90,
            )


@pytest_asyncio.fixture
async def stack(pg_session_factory, minio_store, tmp_path) -> Stack:
    client = await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")
    return Stack(pg_session_factory, minio_store, client, tmp_path)


async def _status(factory, episode_id) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status


async def test_render_parks_at_render_ready_then_reuses_and_versions(stack: Stack) -> None:
    seed = await seed_render_inputs(stack.factory, stack.store)

    first = await stack.run(seed.episode_id)

    assert first.status == EpisodeStatus.RENDER_READY.value
    assert first.final_video is not None and not first.final_video.skipped
    assert await _status(stack.factory, seed.episode_id) is EpisodeStatus.RENDER_READY
    async with stack.factory() as session:
        meta = await ArtifactMetadataRepository(session).get(first.final_video.artifact_id)
    assert meta is not None and meta.version == 1
    payload = await stack.store.get_json(meta.object_key)
    assert sha256_hex(canonical_json_bytes(payload)) == meta.sha256
    final = parse_final_video(payload)
    assert sha256_hex(await stack.store.get_bytes(final.media.object_key)) == final.media.sha256
    assert final.source_production_manifest.artifact_id == seed.manifest.id  # type: ignore[union-attr]

    # 同じ入力の再実行（render_ready → 再入場）は描画しない
    again = await stack.run(seed.episode_id)
    assert again.status == EpisodeStatus.RENDER_READY.value
    assert again.final_video is not None and again.final_video.skipped
    assert again.final_video.artifact_id == first.final_video.artifact_id
    assert stack.engine.completed == 1

    # 別 profile は新しい版、前の版は superseded
    stack.engine.payload = b"long-form-final-video"
    wide = await stack.run(seed.episode_id, "long_form_horizontal")
    assert wide.final_video is not None and wide.final_video.version == 2
    async with stack.factory() as session:
        repo = ArtifactMetadataRepository(session)
        current = await repo.find_current_by_type(seed.episode_id, ArtifactType.FINAL_VIDEO)
        rows = [
            a
            for a in await repo.list_for_episode(seed.episode_id)
            if a.artifact_type is ArtifactType.FINAL_VIDEO
        ]
        jobs = [
            j
            for j in await JobRepository(session).list_for_episode(seed.episode_id)
            if j.type is JobType.RENDER_FINAL_VIDEO
        ]
    assert current is not None and current.id == wide.final_video.artifact_id
    assert len(rows) == 2
    assert sorted(j.status for j in jobs) == sorted(
        [JobStatus.SUCCEEDED, JobStatus.SKIPPED, JobStatus.SUCCEEDED]
    )


async def test_missing_manifest_blocks_the_episode(stack: Stack) -> None:
    seed = await seed_render_inputs(stack.factory, stack.store, with_manifest=False)

    result = await stack.run(seed.episode_id)

    assert result.status == EpisodeStatus.BLOCKED.value
    assert result.failure_class == "needs_input"
    assert stack.engine.requests == []
