"""実PostgreSQLに対するリポジトリ検査（INV-7）。docker compose が必要。

tests/unit/test_repositories.py と同じコードパスを、実DBとAlembicの実スキーマで走らせる。
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)

DATABASE_URL = os.environ.get("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or "postgresql" not in DATABASE_URL,
    reason="DATABASE_URL must point at PostgreSQL (docker compose --profile core up -d)",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    engine = create_async_engine(DATABASE_URL or "")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


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
