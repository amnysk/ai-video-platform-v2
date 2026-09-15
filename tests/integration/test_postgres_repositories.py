"""実PostgreSQLに対するリポジトリ検査（INV-7）。docker compose が必要。

tests/unit/test_repositories.py と同じコードパスを実PostgreSQLで走らせる。
スキーマは `Base.metadata.create_all` で作る（**Alembicは通さない**）。
接続先は ``TEST_DATABASE_URL``（``_test`` DB）だけ。一時スキーマに閉じ込め、最後にスキーマごと消す。
Alembic の実スキーマ検証は tests/integration/test_migration_against_postgres.py が担当する。
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"repo_test_{uuid.uuid4().hex[:12]}"
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


async def test_full_episode_row_lifecycle_on_postgres(pg_session_factory) -> None:
    async with pg_session_factory() as session:
        episodes = EpisodeRepository(session)
        jobs = JobRepository(session)
        artifacts = ArtifactMetadataRepository(session)

        episode = await episodes.create(topic="pg")
        await session.commit()
        assert episode.status is EpisodeStatus.PLANNED

        await episodes.apply_event(episode.id, EpisodeEvent.WORKFLOW_STARTED)
        job = await jobs.create(episode_id=episode.id, type=JobType.DUMMY, max_attempts=3)
        await session.commit()

        await jobs.start(job.id)
        await session.commit()

        digest = "d" * 64
        await artifacts.record(
            episode_id=episode.id,
            artifact_type=ArtifactType.DUMMY,
            schema_version="1.0",
            bucket="artifacts",
            object_key=f"artifacts/{episode.id}/dummy/{digest}.json",
            sha256=digest,
        )
        await jobs.succeed(job.id)
        await episodes.apply_event(episode.id, EpisodeEvent.SKELETON_COMPLETED)
        await session.commit()

        reloaded = await episodes.get(episode.id)
        assert reloaded is not None and reloaded.status is EpisodeStatus.COMPLETED
        assert (await jobs.get(job.id)).status is JobStatus.SUCCEEDED  # type: ignore[union-attr]
        assert len(await artifacts.list_for_episode(episode.id)) == 1


async def test_duplicate_artifact_digest_is_idempotent_on_postgres(pg_session_factory) -> None:
    """UNIQUE制約がアプリのバグより先に二重記録を止める。"""
    async with pg_session_factory() as session:
        episodes = EpisodeRepository(session)
        artifacts = ArtifactMetadataRepository(session)
        episode = await episodes.create(topic="pg")
        await session.commit()

        digest = "e" * 64
        kwargs = {
            "episode_id": episode.id,
            "artifact_type": ArtifactType.DUMMY,
            "schema_version": "1.0",
            "bucket": "artifacts",
            "object_key": f"artifacts/{episode.id}/dummy/{digest}.json",
            "sha256": digest,
        }
        first = await artifacts.record(**kwargs)
        await session.commit()
        second = await artifacts.record(**kwargs)
        await session.commit()

        assert first.id == second.id
        assert len(await artifacts.list_for_episode(episode.id)) == 1


async def test_artifact_generation_a_b_a_on_postgres(pg_session_factory) -> None:
    """A→B→A の復帰が partial unique index（現行は1本）と衝突しないこと。"""
    async with pg_session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        artifacts = ArtifactMetadataRepository(session)

        def kwargs(sha: str, input_hash: str) -> dict:
            return dict(
                episode_id=episode.id,
                artifact_type=ArtifactType.STORYBOARD,
                schema_version="1.0",
                bucket="artifacts",
                object_key=f"artifacts/{episode.id}/storyboard/{sha}.json",
                sha256=sha,
                input_hash=input_hash,
            )

        first = await artifacts.record(**kwargs("a" * 64, "h1"))
        await session.commit()
        await artifacts.record(**kwargs("b" * 64, "h2"))
        await session.commit()
        again = await artifacts.record(**kwargs("a" * 64, "h1"))
        await session.commit()

        current = await artifacts.find_current_by_type(episode.id, ArtifactType.STORYBOARD)
        assert current is not None and current.id == first.id == again.id
