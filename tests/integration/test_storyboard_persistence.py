"""実PostgreSQL + 実MinIO で storyboard の永続化を検査する（ADR-0012 / ADR-0015）。

docker compose が必要。**共有 DB を壊さない**: 一時スキーマを作って ``search_path`` で閉じ込め、
最後にスキーマごと消す（Alembic は通さない。スキーマは ``Base.metadata.create_all``）。
"""

from __future__ import annotations

import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.artifacts import StoryboardArtifact, parse_artifact
from contracts.states import ArtifactType, EpisodeStatus
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from infrastructure.db.models import Base
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from tests.support.fakes import FakeStoryboardGenerator
from tests.support.storyboard import (
    BUCKET,
    PROMPT_ID,
    PROMPT_VERSION,
    artifact_rows,
    create_episode_at_script_ready,
    good_storyboard,
    record_script,
)
from workers.storyboard.activities import (
    CreateJobRequest,
    EpisodeRef,
    GenerateStoryboardRequest,
    StoryboardActivities,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or "postgresql" not in DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="DATABASE_URL (PostgreSQL) and MINIO_ENDPOINT must be set (docker compose core)",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"sb_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    return store


async def _run(session_factory, store, episode_id: str, generator) -> None:
    activities = StoryboardActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket=BUCKET,
        timeout_seconds=5,
        prompt_template_id=PROMPT_ID,
        prompt_template_version=PROMPT_VERSION,
    )
    admit = await activities.admit_episode(EpisodeRef(episode_id=episode_id))
    assert admit.admitted
    job_id = await activities.create_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )
    await activities.mark_ready(EpisodeRef(episode_id=episode_id))


async def test_storyboard_roundtrip_and_versioning_against_real_services(
    pg_session_factory, minio_store
) -> None:
    episode_id = await create_episode_at_script_ready(pg_session_factory, minio_store)
    await _run(
        pg_session_factory,
        minio_store,
        episode_id,
        FakeStoryboardGenerator(output=good_storyboard()),
    )

    async with pg_session_factory() as session:
        first = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.STORYBOARD
        )
        episode = await EpisodeRepository(session).get(episode_id)
    assert first is not None
    assert episode is not None and episode.status is EpisodeStatus.STORYBOARD_READY
    payload = await minio_store.get_json(first.object_key)
    assert sha256_hex(canonical_json_bytes(payload)) == first.sha256
    assert isinstance(parse_artifact(payload), StoryboardArtifact)

    # 台本の新しい世代 → storyboard も新しい世代、古い世代は superseded
    await record_script(pg_session_factory, minio_store, episode_id, title="改稿")
    await _run(
        pg_session_factory,
        minio_store,
        episode_id,
        FakeStoryboardGenerator(output=good_storyboard(description="改稿の土器")),
    )
    rows = await artifact_rows(pg_session_factory, episode_id, ArtifactType.STORYBOARD)
    by_version = {a.version: a for a in rows}
    assert set(by_version) == {1, 2}
    assert by_version[1].superseded_at is not None
    assert by_version[2].superseded_at is None
    second = await minio_store.get_json(by_version[2].object_key)
    assert sha256_hex(canonical_json_bytes(second)) == by_version[2].sha256
