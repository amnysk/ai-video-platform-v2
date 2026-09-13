"""StoryboardWorkflow の縦切り（Temporal + DB + ArtifactStore + 生成器 / ADR-0015）。

**本物の Codex・OpenMontage は呼ばない。** ``FakeStoryboardGenerator`` を注入する（INV-18）。
"""

from __future__ import annotations

import itertools

import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.artifacts import StoryboardArtifact, parse_artifact
from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    JobStatus,
    JobType,
    ProviderCall,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import ProviderUnavailableError, StoryboardSchemaViolationError
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryboardGenerator
from tests.support.storyboard import (
    BUCKET,
    PROMPT_ID,
    PROMPT_VERSION,
    UNCOVERED_STORYBOARD,
    artifact_rows,
    create_episode_at_script_ready,
    good_storyboard,
    record_script,
)
from workers.storyboard.activities import StoryboardActivities
from workers.storyboard.workflows import StoryboardWorkflow, StoryboardWorkflowInput

TASK_QUEUE = "storyboard-test"
_run_ids = itertools.count()


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class StoryboardHarness:
    def __init__(self, env: WorkflowEnvironment, session_factory) -> None:
        self._env = env
        self.session_factory = session_factory
        self.store = InMemoryArtifactStore()
        self.episode_id = ""

    async def seed(self, *, with_script: bool = True) -> str:
        self.episode_id = await create_episode_at_script_ready(
            self.session_factory, self.store, with_script=with_script
        )
        return self.episode_id

    async def run(self, generator: FakeStoryboardGenerator, *, max_attempts: int = 3):
        if not self.episode_id:
            await self.seed()
        activities = StoryboardActivities(
            session_factory=self.session_factory,
            store=self.store,
            generator=generator,
            bucket=BUCKET,
            timeout_seconds=5,
            prompt_template_id=PROMPT_ID,
            prompt_template_version=PROMPT_VERSION,
        )
        client: Client = self._env.client
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[StoryboardWorkflow],
            activities=activities.all_activities(),
        ):
            return await client.execute_workflow(
                StoryboardWorkflow.run,
                StoryboardWorkflowInput(episode_id=self.episode_id, max_attempts=max_attempts),
                id=f"episode-{self.episode_id}-storyboard-{next(_run_ids)}",
                task_queue=TASK_QUEUE,
            )

    async def state(self):
        async with self.session_factory() as session:
            return (
                await EpisodeRepository(session).get(self.episode_id),
                [
                    j
                    for j in await JobRepository(session).list_for_episode(self.episode_id)
                    if j.type is JobType.PLAN_STORYBOARD
                ],
                [
                    a
                    for a in await ArtifactMetadataRepository(session).list_for_episode(
                        self.episode_id
                    )
                    if a.artifact_type is ArtifactType.STORYBOARD
                ],
            )


@pytest_asyncio.fixture
async def harness(env, session_factory) -> StoryboardHarness:
    return StoryboardHarness(env, session_factory)


async def test_storyboard_is_generated_stored_and_episode_becomes_storyboard_ready(
    harness,
) -> None:
    generator = FakeStoryboardGenerator(output=good_storyboard())
    result = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 1
    assert result.status == EpisodeStatus.STORYBOARD_READY.value
    assert result.rounds_used == 1 and result.reused_existing_artifact is False
    assert episode is not None and episode.status is EpisodeStatus.STORYBOARD_READY
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED]
    assert len(artifacts) == 1
    meta = artifacts[0]
    (row,) = await artifact_rows(
        harness.session_factory, harness.episode_id, ArtifactType.STORYBOARD
    )
    assert row.input_hash and row.input_hash != meta.sha256
    stored = await harness.store.get_json(meta.object_key)
    assert sha256_hex(canonical_json_bytes(stored)) == meta.sha256 == result.sha256
    assert isinstance(parse_artifact(stored), StoryboardArtifact)


async def test_rerun_skips_the_job_and_reuses_the_same_artifact(harness) -> None:
    generator = FakeStoryboardGenerator(output=good_storyboard())
    first = await harness.run(generator)
    second = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 1, "同じ入力で2度目の課金呼び出しをしない"
    assert second.reused_existing_artifact is True
    assert second.sha256 == first.sha256
    assert episode is not None and episode.status is EpisodeStatus.STORYBOARD_READY
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED, JobStatus.SKIPPED]
    assert len(artifacts) == 1


async def test_new_script_version_produces_a_new_storyboard_version(harness) -> None:
    generator = FakeStoryboardGenerator(output=good_storyboard())
    first = await harness.run(generator)
    await record_script(harness.session_factory, harness.store, harness.episode_id, title="改稿")
    second = await harness.run(
        FakeStoryboardGenerator(output=good_storyboard(description="改稿の土器"))
    )
    rows = await artifact_rows(harness.session_factory, harness.episode_id, ArtifactType.STORYBOARD)

    assert second.reused_existing_artifact is False
    assert second.sha256 != first.sha256
    by_sha = {a.sha256: a for a in rows}
    assert by_sha[first.sha256].superseded_at is not None
    assert by_sha[second.sha256].superseded_at is None
    assert by_sha[second.sha256].version == by_sha[first.sha256].version + 1
    stored = parse_artifact(await harness.store.get_json(by_sha[second.sha256].object_key))
    async with harness.session_factory() as session:
        script = await ArtifactMetadataRepository(session).find_current_by_type(
            harness.episode_id, ArtifactType.SCRIPT
        )
    assert isinstance(stored, StoryboardArtifact) and script is not None
    assert stored.source_script.sha256 == script.sha256


async def test_missing_script_blocks_without_calling_the_generator(harness) -> None:
    await harness.seed(with_script=False)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    result = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 0
    assert result.status == EpisodeStatus.BLOCKED.value
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert [j.status for j in jobs] == [JobStatus.TERMINAL_FAILED]
    assert artifacts == []


async def test_output_defect_is_retried_in_a_new_round(harness) -> None:
    outputs = iter([UNCOVERED_STORYBOARD, good_storyboard()])
    generator = FakeStoryboardGenerator(output=lambda _r: next(outputs))
    result = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 2 and result.rounds_used == 2
    assert episode is not None and episode.status is EpisodeStatus.STORYBOARD_READY
    assert [j.status for j in jobs] == [JobStatus.SUCCEEDED]
    assert len(artifacts) == 1
    async with harness.session_factory() as session:
        rows = await ProviderReservationRepository(session).find_unreconciled(
            episode_id=harness.episode_id, provider=ProviderCall.CODEX_STORYBOARD
        )
    assert rows == []


async def test_exhausted_rounds_block_instead_of_failing(harness) -> None:
    generator = FakeStoryboardGenerator(
        fail_times=99, error=StoryboardSchemaViolationError("always bad")
    )
    result = await harness.run(generator, max_attempts=2)
    episode, jobs, _ = await harness.state()

    assert generator.calls == 2
    assert result.status == EpisodeStatus.BLOCKED.value
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert [j.status for j in jobs] == [JobStatus.TERMINAL_FAILED]


async def test_needs_input_error_stops_without_extra_rounds(harness) -> None:
    generator = FakeStoryboardGenerator(fail_times=99, error=ProviderUnavailableError("no codex"))
    result = await harness.run(generator, max_attempts=3)
    episode, jobs, _ = await harness.state()

    assert generator.calls == 1
    assert result.status == EpisodeStatus.BLOCKED.value
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert [j.status for j in jobs] == [JobStatus.TERMINAL_FAILED]


async def test_unreconciled_reservation_blocks_without_calling(harness) -> None:
    episode_id = await harness.seed()
    async with harness.session_factory() as session:
        repo = ProviderReservationRepository(session)
        stale = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.CODEX_STORYBOARD,
            idempotency_key="stale-key-from-a-crashed-worker",
            input_hash="old",
            round=1,
        )
        await repo.mark_dispatched(stale.id)
        await session.commit()

    generator = FakeStoryboardGenerator(output=good_storyboard())
    result = await harness.run(generator)
    episode, _, artifacts = await harness.state()

    assert generator.calls == 0
    assert result.status == EpisodeStatus.BLOCKED.value
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert artifacts == []


async def test_episode_outside_parking_points_is_not_admitted(harness, session_factory) -> None:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="planned only")
        await session.commit()
    harness.episode_id = episode.id

    generator = FakeStoryboardGenerator(output=good_storyboard())
    result = await harness.run(generator)
    current, jobs, artifacts = await harness.state()

    assert result.admitted is False
    assert result.status == EpisodeStatus.PLANNED.value
    assert current is not None and current.status is EpisodeStatus.PLANNED
    assert generator.calls == 0 and jobs == [] and artifacts == []
