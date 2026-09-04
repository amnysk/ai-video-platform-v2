"""Job の `skipped` 記録（docs/domain/job.md）。

`skipped` は「入力hashが一致する出力Artifactが既に存在したため実処理をせずに
既存Artifactを返した」ことの記録であり、**再開が効いた証拠**である。
これが記録されないなら冪等性が壊れている。

Phase 1 の骨組みには skip 経路が無く、Script Worker が最初の実装者になる。
"""

from __future__ import annotations

import pytest

from contracts.states import JobStatus, JobType
from domain.errors import InvalidTransitionError
from infrastructure.db.repositories import EpisodeRepository, JobRepository


async def test_queued_job_can_be_skipped_without_running(session) -> None:
    """生成器を呼ばずに既存Artifactを返した場合、Jobは running を経ずに skipped。"""
    episodes = EpisodeRepository(session)
    jobs = JobRepository(session)
    episode = await episodes.create(topic="t")
    job = await jobs.create(episode_id=episode.id, type=JobType.WRITE_SCRIPT, max_attempts=3)
    await session.commit()

    skipped = await jobs.mark_skipped(job.id)
    await session.commit()

    assert skipped.status is JobStatus.SKIPPED
    assert skipped.attempts == 0, "skip は試行を消費しない（課金呼び出しが起きていない）"


async def test_running_job_can_be_skipped(session) -> None:
    episodes = EpisodeRepository(session)
    jobs = JobRepository(session)
    episode = await episodes.create(topic="t")
    job = await jobs.create(episode_id=episode.id, type=JobType.WRITE_SCRIPT, max_attempts=3)
    await session.commit()
    await jobs.start(job.id)
    await session.commit()

    skipped = await jobs.mark_skipped(job.id)
    await session.commit()
    assert skipped.status is JobStatus.SKIPPED


async def test_succeeded_job_cannot_be_skipped(session) -> None:
    """terminal から出る遷移は無い。"""
    episodes = EpisodeRepository(session)
    jobs = JobRepository(session)
    episode = await episodes.create(topic="t")
    job = await jobs.create(episode_id=episode.id, type=JobType.WRITE_SCRIPT, max_attempts=3)
    await session.commit()
    await jobs.start(job.id)
    await jobs.succeed(job.id)
    await session.commit()

    with pytest.raises(InvalidTransitionError):
        await jobs.mark_skipped(job.id)
