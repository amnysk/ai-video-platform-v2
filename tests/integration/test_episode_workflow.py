"""EpisodeSkeletonWorkflow の縦切り（Temporal + DB + ArtifactStore）。

Temporalの time-skipping テスト環境を使う。有料provider・実投稿へは
一切到達しない（INV-18）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.artifacts import DUMMY_ARTIFACT_SCHEMA_VERSION, parse_artifact
from contracts.states import EpisodeStatus, JobStatus, JobType
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import NeedsInputError, PermanentError, TransientError
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
)
from infrastructure.storage.artifact_store import PutResult
from infrastructure.storage.memory_store import InMemoryArtifactStore
from workers.dummy.activities import DummyActivities
from workers.dummy.workflows import EpisodeSkeletonWorkflow, EpisodeWorkflowInput

TASK_QUEUE = "dummy-test"
BUCKET = "artifacts"


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class FlakyArtifactStore:
    """最初の ``fail_times`` 回だけ保存に失敗する ArtifactStore。

    Activityが Job を running にした**後**、作業の最中に落ちる形を再現する
    （現実のMinIO障害・provider障害と同じ位置）。
    """

    def __init__(
        self,
        inner: InMemoryArtifactStore,
        *,
        fail_times: int = 0,
        error: Exception | None = None,
        on_call: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.inner = inner
        self.fail_times = fail_times
        self.error = error
        self.on_call = on_call
        self.calls = 0

    async def put_json(self, key: str, payload: Mapping[str, Any]) -> PutResult:
        self.calls += 1
        if self.on_call is not None:
            await self.on_call()
        if self.calls <= self.fail_times and self.error is not None:
            raise self.error
        return await self.inner.put_json(key, payload)

    async def get_json(self, key: str) -> dict[str, Any]:
        return await self.inner.get_json(key)

    async def exists(self, key: str) -> bool:
        return await self.inner.exists(key)

    def write_count(self, key: str) -> int:
        return self.inner.write_count(key)


class WorkflowHarness:
    """Episodeを1本作り、workerを立て、workflowを完走させる。"""

    def __init__(self, env: WorkflowEnvironment, session_factory) -> None:
        self._env = env
        self._session_factory = session_factory
        self.episode_id: str = ""
        self.observed_episode_statuses: list[EpisodeStatus] = []

    async def record_episode_status(self) -> None:
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).get(self.episode_id)
            assert episode is not None
            self.observed_episode_statuses.append(episode.status)

    async def run(self, store, *, max_attempts: int = 3):
        async with self._session_factory() as session:
            episode = await EpisodeRepository(session).create(topic="dummy")
            await session.commit()
            self.episode_id = str(episode.id)

        activities = DummyActivities(
            session_factory=self._session_factory, store=store, bucket=BUCKET
        )
        client: Client = self._env.client
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[EpisodeSkeletonWorkflow],
            activities=activities.all_activities(),
        ):
            return await client.execute_workflow(
                EpisodeSkeletonWorkflow.run,
                EpisodeWorkflowInput(episode_id=self.episode_id, max_attempts=max_attempts),
                id=f"episode-{self.episode_id}",
                task_queue=TASK_QUEUE,
            )

    async def state(self):
        async with self._session_factory() as session:
            return (
                await EpisodeRepository(session).get(self.episode_id),
                await JobRepository(session).list_for_episode(self.episode_id),
                await ArtifactMetadataRepository(session).list_for_episode(self.episode_id),
            )


@pytest_asyncio.fixture
async def harness(env, session_factory) -> WorkflowHarness:
    return WorkflowHarness(env, session_factory)


async def test_workflow_completes_and_persists_everything(harness) -> None:
    """完了条件3〜8の縦切り: workflow実行 → MinIO保存 → DB記録 → completed。"""
    store = InMemoryArtifactStore()
    result = await harness.run(store)
    episode, jobs, artifacts = await harness.state()

    assert episode is not None
    assert episode.status is EpisodeStatus.COMPLETED
    assert result.status == EpisodeStatus.COMPLETED.value

    assert len(jobs) == 1
    assert jobs[0].type is JobType.DUMMY
    assert jobs[0].status is JobStatus.SUCCEEDED
    assert jobs[0].attempts == 1

    assert len(artifacts) == 1
    meta = artifacts[0]
    assert meta.bucket == BUCKET
    assert meta.schema_version == DUMMY_ARTIFACT_SCHEMA_VERSION
    assert meta.object_key == result.artifact_object_key

    stored = await store.get_json(meta.object_key)
    parsed = parse_artifact(stored)
    assert parsed.episode_id == harness.episode_id
    assert parsed.message == "workflow completed"
    assert meta.sha256 == sha256_hex(canonical_json_bytes(stored)) == result.sha256


async def test_activity_fails_once_then_retry_succeeds(harness) -> None:
    """故障試験1: 1回失敗 → retryで成功。Episodeはterminal failedにならない（INV-12）。"""
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=1, error=TransientError("simulated MinIO hiccup")
    )
    result = await harness.run(store)
    episode, jobs, artifacts = await harness.state()

    assert store.calls == 2
    assert episode is not None
    assert episode.status is EpisodeStatus.COMPLETED
    assert result.status == EpisodeStatus.COMPLETED.value
    assert jobs[0].status is JobStatus.SUCCEEDED
    assert jobs[0].attempts == 2, "各試行で attempts が加算される"
    assert len(artifacts) == 1, "再実行でArtifactメタデータが重複しない (INV-17)"


async def test_episode_is_not_completed_while_temporal_is_retrying(harness) -> None:
    """故障試験3: retry中にEpisodeがcompletedにならない。"""
    store = FlakyArtifactStore(
        InMemoryArtifactStore(),
        fail_times=1,
        error=TransientError("simulated MinIO hiccup"),
        on_call=harness.record_episode_status,
    )
    await harness.run(store)

    assert harness.observed_episode_statuses == [
        EpisodeStatus.IN_PROGRESS,
        EpisodeStatus.IN_PROGRESS,
    ]
    assert EpisodeStatus.COMPLETED not in harness.observed_episode_statuses


async def test_reexecuted_activity_does_not_corrupt_the_artifact(harness) -> None:
    """故障試験2: 同じActivityが再実行されてもArtifactが壊れない。"""
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=2, error=TransientError("simulated MinIO hiccup")
    )
    await harness.run(store)
    _, _, artifacts = await harness.state()

    assert store.calls == 3
    assert len(artifacts) == 1
    key = artifacts[0].object_key
    assert store.write_count(key) == 1, "immutableなオブジェクトを書き直していない (INV-11)"
    stored = await store.get_json(key)
    assert parse_artifact(stored).episode_id == harness.episode_id
    assert artifacts[0].sha256 == sha256_hex(canonical_json_bytes(stored))


async def test_exhausted_retryable_failure_blocks_instead_of_failing(harness) -> None:
    """INV-12: retry可能な失敗を使い切ってもEpisodeを terminal failed にしない。"""
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=99, error=TransientError("always failing")
    )
    result = await harness.run(store, max_attempts=2)
    episode, jobs, artifacts = await harness.state()

    assert store.calls == 2, "max_attempts を超えてretryしない"
    assert episode is not None
    assert episode.status is EpisodeStatus.BLOCKED
    assert episode.status is not EpisodeStatus.FAILED
    assert result.status == EpisodeStatus.BLOCKED.value
    assert jobs[0].status is JobStatus.TERMINAL_FAILED
    assert jobs[0].attempts == 2
    assert artifacts == []


async def test_permanent_failure_is_not_retried_and_fails_the_episode(harness) -> None:
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=99, error=PermanentError("structurally invalid")
    )
    result = await harness.run(store, max_attempts=3)
    episode, jobs, _ = await harness.state()

    assert store.calls == 1, "permanent失敗はretryしない"
    assert episode is not None and episode.status is EpisodeStatus.FAILED
    assert result.status == EpisodeStatus.FAILED.value
    assert jobs[0].status is JobStatus.TERMINAL_FAILED


async def test_needs_input_failure_blocks_for_a_human(harness) -> None:
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=99, error=NeedsInputError("needs a human")
    )
    await harness.run(store, max_attempts=3)
    episode, jobs, _ = await harness.state()

    assert store.calls == 1
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert jobs[0].status is JobStatus.TERMINAL_FAILED


async def test_unclassified_failure_blocks_instead_of_failing(harness) -> None:
    """INV-12: 分類できない失敗は自動修復に流さず人間へ。terminal failed にしない。"""
    store = FlakyArtifactStore(
        InMemoryArtifactStore(), fail_times=99, error=ValueError("who knows")
    )
    result = await harness.run(store, max_attempts=2)
    episode, jobs, _ = await harness.state()

    assert episode is not None
    assert episode.status is EpisodeStatus.BLOCKED
    assert episode.status is not EpisodeStatus.FAILED
    assert result.status == EpisodeStatus.BLOCKED.value
    assert jobs[0].status is JobStatus.TERMINAL_FAILED
