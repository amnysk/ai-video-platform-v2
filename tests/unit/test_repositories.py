"""リポジトリの永続化（INV-7）。

SQLiteに対して実行する。同じコードを実PostgreSQLに対して走らせる版は
tests/integration/test_postgres_repositories.py（-m integration）。
"""

from __future__ import annotations

import pytest

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)


async def test_create_and_load_episode(session) -> None:
    episodes = EpisodeRepository(session)
    episode = await episodes.create(topic="dummy topic")
    await session.commit()

    loaded = await episodes.get(episode.id)
    assert loaded is not None
    assert loaded.status is EpisodeStatus.PLANNED
    assert loaded.created_at is not None
    assert loaded.updated_at is not None


async def test_apply_event_persists_new_status(session) -> None:
    from domain.episode.transitions import EpisodeEvent

    episodes = EpisodeRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    await episodes.apply_event(episode.id, EpisodeEvent.WORKFLOW_STARTED)
    await session.commit()

    reloaded = await episodes.get(episode.id)
    assert reloaded is not None
    assert reloaded.status is EpisodeStatus.IN_PROGRESS


async def test_apply_invalid_event_raises_and_leaves_status_untouched(session) -> None:
    from domain.episode.transitions import EpisodeEvent
    from domain.errors import InvalidTransitionError

    episodes = EpisodeRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    with pytest.raises(InvalidTransitionError):
        await episodes.apply_event(episode.id, EpisodeEvent.SKELETON_COMPLETED)
    await session.rollback()

    reloaded = await episodes.get(episode.id)
    assert reloaded is not None
    assert reloaded.status is EpisodeStatus.PLANNED


async def test_job_lifecycle_is_persisted(session) -> None:
    episodes = EpisodeRepository(session)
    jobs = JobRepository(session)
    episode = await episodes.create(topic="t")
    job = await jobs.create(episode_id=episode.id, type=JobType.DUMMY, max_attempts=3)
    await session.commit()

    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.max_attempts == 3

    await jobs.start(job.id)
    await session.commit()
    started = await jobs.get(job.id)
    assert started is not None
    assert started.status is JobStatus.RUNNING
    assert started.attempts == 1

    await jobs.succeed(job.id)
    await session.commit()
    done = await jobs.get(job.id)
    assert done is not None
    assert done.status is JobStatus.SUCCEEDED


async def test_artifact_metadata_is_recorded_with_digest(session) -> None:
    episodes = EpisodeRepository(session)
    artifacts = ArtifactMetadataRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    digest = "a" * 64
    meta = await artifacts.record(
        episode_id=episode.id,
        artifact_type=ArtifactType.DUMMY,
        schema_version="1.0",
        bucket="artifacts",
        object_key=f"artifacts/{episode.id}/dummy/{digest}.json",
        sha256=digest,
    )
    await session.commit()

    rows = await artifacts.list_for_episode(episode.id)
    assert [r.id for r in rows] == [meta.id]
    assert rows[0].sha256 == digest
    assert rows[0].schema_version == "1.0"


async def test_recording_the_same_artifact_twice_is_idempotent(session) -> None:
    """INV-17: Activityの再実行でメタデータが重複しない。"""
    episodes = EpisodeRepository(session)
    artifacts = ArtifactMetadataRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    digest = "b" * 64
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


async def test_recording_the_current_content_with_a_new_input_hash_updates_it(session) -> None:
    """同じ sha256 の現行行に別の input_hash が明示されたら、その行の input_hash を更新する。"""
    episodes = EpisodeRepository(session)
    artifacts = ArtifactMetadataRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    digest = "c" * 64
    kwargs = {
        "episode_id": episode.id,
        "artifact_type": ArtifactType.DUMMY,
        "schema_version": "1.0",
        "bucket": "artifacts",
        "object_key": f"artifacts/{episode.id}/dummy/{digest}.json",
        "sha256": digest,
    }
    first = await artifacts.record(**kwargs, input_hash="h1")
    await session.commit()
    second = await artifacts.record(**kwargs, input_hash="h2")
    await session.commit()
    omitted = await artifacts.record(**kwargs)
    await session.commit()

    assert first.id == second.id == omitted.id

    async def _current(input_hash: str):
        return await artifacts.find_current(
            episode_id=episode.id, artifact_type=ArtifactType.DUMMY, input_hash=input_hash
        )

    found = await _current("h2")
    assert found is not None and found.id == first.id, "input_hash を省略した再記録は値を変えない"
    assert await _current("h1") is None
    assert len(await artifacts.list_for_episode(episode.id)) == 1
