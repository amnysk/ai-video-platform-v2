"""ScriptWorkflow の縦切り（Temporal + DB + ArtifactStore + 生成器）。

**本物の Codex は呼ばない。** `FakeStoryGenerator` を注入する（INV-18）。
Phase 1 の `test_episode_workflow.py` と同じ故障注入の作法を踏襲する。
"""

from __future__ import annotations

import json
from typing import Any

import pytest_asyncio
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.artifacts import (
    SCRIPT_ARTIFACT_SCHEMA_VERSION,
    ScriptArtifact,
    parse_artifact,
)
from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType, ProviderCall
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.errors import (
    ProviderTimeoutError,
    ProviderUnavailableError,
    ScriptSchemaViolationError,
)
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from workers.planning.activities import ScriptActivities
from workers.planning.workflows import ScriptWorkflow, ScriptWorkflowInput

TASK_QUEUE = "script-test"
BUCKET = "artifacts"


def good_script(*, title: str = "縄文の火") -> str:
    """契約を満たす台本JSON。Codexが返す形（episode_id等は呼び出し側が注入）。"""
    return json.dumps(
        {
            "language": "ja",
            "title": title,
            "hook": "この土器、なぜ焦げているのか",
            "scenes": [
                {
                    "id": "s1",
                    "narration": "縄文土器には焦げ跡が残る。",
                    "visual": "土器のクローズアップ",
                    "duration_ms": 8000,
                },
                {
                    "id": "s2",
                    "narration": "煮炊きに使われた証拠だ。",
                    "visual": "復元された炉",
                    "duration_ms": 9000,
                },
                {
                    "id": "s3",
                    "narration": "食が定住を支えた。",
                    "visual": "集落の俯瞰",
                    "duration_ms": 8000,
                },
            ],
        },
        ensure_ascii=False,
    )


FENCED_SCRIPT = f"```json\n{good_script()}\n```"


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    yield environment
    await environment.shutdown()


class ScriptHarness:
    def __init__(self, env: WorkflowEnvironment, session_factory) -> None:
        self._env = env
        self._session_factory = session_factory
        self.episode_id = ""
        self.store = InMemoryArtifactStore()

    async def run(
        self, generator: FakeStoryGenerator, *, max_attempts: int = 3, topic: str = "縄文土器"
    ):
        if not self.episode_id:
            async with self._session_factory() as session:
                episode = await EpisodeRepository(session).create(topic=topic)
                await session.commit()
                self.episode_id = str(episode.id)

        activities = ScriptActivities(
            session_factory=self._session_factory,
            store=self.store,
            generator=generator,
            bucket=BUCKET,
            generator_id="fake",
            model="fake-model",
            timeout_seconds=5,
        )
        client: Client = self._env.client
        async with Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[ScriptWorkflow],
            activities=activities.all_activities(),
        ):
            return await client.execute_workflow(
                ScriptWorkflow.run,
                ScriptWorkflowInput(episode_id=self.episode_id, max_attempts=max_attempts),
                id=f"script-{self.episode_id}-{generator.calls}-{id(generator)}",
                task_queue=TASK_QUEUE,
            )

    async def state(self):
        async with self._session_factory() as session:
            return (
                await EpisodeRepository(session).get(self.episode_id),
                await JobRepository(session).list_for_episode(self.episode_id),
                await ArtifactMetadataRepository(session).list_for_episode(self.episode_id),
            )

    async def reservations(self) -> list[Any]:
        async with self._session_factory() as session:
            repo = ProviderReservationRepository(session)
            return list(
                await repo.find_unreconciled(
                    episode_id=self.episode_id, provider=ProviderCall.CODEX_SCRIPT
                )
            )


@pytest_asyncio.fixture
async def harness(env, session_factory) -> ScriptHarness:
    return ScriptHarness(env, session_factory)


async def test_script_is_generated_validated_stored_and_episode_becomes_script_ready(
    harness,
) -> None:
    """Phase 2 の完了条件の縦切り。"""
    generator = FakeStoryGenerator(output=FENCED_SCRIPT)
    result = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 1
    assert episode is not None
    assert episode.status is EpisodeStatus.SCRIPT_READY
    assert result.status == EpisodeStatus.SCRIPT_READY.value
    assert result.rounds_used == 1
    assert result.reused_existing_artifact is False

    assert [j.type for j in jobs] == [JobType.WRITE_SCRIPT]
    assert jobs[0].status is JobStatus.SUCCEEDED
    assert jobs[0].attempts == 1

    assert len(artifacts) == 1
    meta = artifacts[0]
    assert meta.artifact_type is ArtifactType.SCRIPT
    assert meta.schema_version == SCRIPT_ARTIFACT_SCHEMA_VERSION
    assert meta.object_key == result.artifact_object_key

    stored = await harness.store.get_json(meta.object_key)
    parsed = parse_artifact(stored)
    assert isinstance(parsed, ScriptArtifact), (
        "script payload は ScriptArtifact へディスパッチされる"
    )
    assert parsed.episode_id == harness.episode_id
    assert parsed.title == "縄文の火"
    assert len(parsed.scenes) == 3
    assert meta.sha256 == sha256_hex(canonical_json_bytes(stored)) == result.sha256


async def test_no_dummy_job_is_created_by_the_script_workflow(harness) -> None:
    """Phase 1 の worker と状態を共有しても、工程が混ざらないこと。"""
    await harness.run(FakeStoryGenerator(output=good_script()))
    _, jobs, _ = await harness.state()
    assert all(job.type is not JobType.DUMMY for job in jobs)


async def test_transient_failure_retries_in_a_new_round_and_succeeds(harness) -> None:
    """故障試験1: 1回失敗 → 次ラウンドで成功。terminal failed にしない（INV-12）。"""
    generator = FakeStoryGenerator(
        output=good_script(), fail_times=1, error=ProviderTimeoutError("simulated timeout")
    )
    result = await harness.run(generator)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 2
    assert result.rounds_used == 2
    assert episode is not None and episode.status is EpisodeStatus.SCRIPT_READY
    assert jobs[0].status is JobStatus.SUCCEEDED
    assert len(artifacts) == 1, "retryでArtifactが重複しない"


async def test_schema_violation_is_retryable_not_permanent(harness) -> None:
    """ADR-0014: LLM出力の形式不正は retryable。1回の生成揺れで作品を失わない。"""
    outputs = iter(['{"language":"ja","title":"x"}', good_script()])

    generator = FakeStoryGenerator(output=lambda _req: next(outputs))
    result = await harness.run(generator)
    episode, _, artifacts = await harness.state()

    assert generator.calls == 2
    assert episode is not None and episode.status is EpisodeStatus.SCRIPT_READY
    assert result.rounds_used == 2
    assert len(artifacts) == 1


async def test_unparseable_output_is_retryable(harness) -> None:
    outputs = iter(["ここに台本はありません", good_script()])
    generator = FakeStoryGenerator(output=lambda _req: next(outputs))
    await harness.run(generator)
    episode, _, artifacts = await harness.state()
    assert generator.calls == 2
    assert episode is not None and episode.status is EpisodeStatus.SCRIPT_READY
    assert len(artifacts) == 1


async def test_exhausted_rounds_block_instead_of_failing(harness) -> None:
    """INV-12: retry枠を使い切ってもEpisodeを terminal failed にしない。"""
    generator = FakeStoryGenerator(
        output=good_script(), fail_times=99, error=ScriptSchemaViolationError("always bad")
    )
    result = await harness.run(generator, max_attempts=2)
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 2, "max_attempts を超えて課金呼び出しをしない"
    assert episode is not None
    assert episode.status is EpisodeStatus.BLOCKED
    assert episode.status is not EpisodeStatus.FAILED
    assert result.status == EpisodeStatus.BLOCKED.value
    assert jobs[0].status is JobStatus.TERMINAL_FAILED
    assert artifacts == []


async def test_needs_input_failure_stops_immediately_without_more_paid_calls(harness) -> None:
    """CLI不在・未認証は同じ入力で繰り返しても変わらない。課金を増やさず blocked へ。"""
    generator = FakeStoryGenerator(
        output=good_script(), fail_times=99, error=ProviderUnavailableError("codex not found")
    )
    result = await harness.run(generator, max_attempts=3)
    episode, jobs, _ = await harness.state()

    assert generator.calls == 1, "needs_input はラウンドを重ねない"
    assert episode is not None and episode.status is EpisodeStatus.BLOCKED
    assert result.status == EpisodeStatus.BLOCKED.value
    assert jobs[0].status is JobStatus.TERMINAL_FAILED


async def test_unclassified_failure_blocks_instead_of_failing(harness) -> None:
    generator = FakeStoryGenerator(
        output=good_script(), fail_times=99, error=ValueError("who knows")
    )
    result = await harness.run(generator, max_attempts=2)
    episode, _, _ = await harness.state()

    assert episode is not None
    assert episode.status is EpisodeStatus.BLOCKED
    assert episode.status is not EpisodeStatus.FAILED
    assert result.status == EpisodeStatus.BLOCKED.value


async def test_reservation_is_reconciled_and_linked_to_the_artifact(harness) -> None:
    """ADR-0013: 呼び出しが成立したら予約は spent になり、未照合が残らない。"""
    await harness.run(FakeStoryGenerator(output=good_script()))
    assert await harness.reservations() == [], "未照合の予約が残っていない"


async def test_metadata_records_the_model_that_actually_generated(harness) -> None:
    """再現性のため、実際に生成したモデルを記録する（`generator_model` は必須）。

    設定でモデルを固定しない運用（codex 自身の既定に従う）でも、
    ``generator_model`` が空になってはならない ── 空だと契約違反で
    せっかく生成された台本が捨てられる（実環境で踏んだ）。
    """
    generator = FakeStoryGenerator(output=good_script(), model="codex-config-default")
    await harness.run(generator)
    _, _, artifacts = await harness.state()

    stored = await harness.store.get_json(artifacts[0].object_key)
    parsed = parse_artifact(stored)
    assert isinstance(parsed, ScriptArtifact)
    assert parsed.metadata.generator_model == "codex-config-default"
    assert parsed.metadata.generator == "fake"


async def test_rerunning_the_workflow_reuses_the_artifact_without_calling_the_generator(
    harness,
) -> None:
    """同じ入力での再実行は**生成器を呼ばない**（ADR-0012 / INV-17）。

    Phase 2 の最重要要件。実機で `script_ready` の Episode を再実行したとき
    状態遷移で弾かれてワークフローが落ちた（遷移表は正しく、冪等性の実装が
    欠けていた）ので、ここで固定する。
    """
    generator = FakeStoryGenerator(output=good_script())
    await harness.run(generator)
    assert generator.calls == 1

    result = await harness.run(generator)  # 2回目
    episode, jobs, artifacts = await harness.state()

    assert generator.calls == 1, "同じ入力で2度目の課金呼び出しをしない"
    assert result.reused_existing_artifact is True
    assert result.status == EpisodeStatus.SCRIPT_READY.value
    assert episode is not None and episode.status is EpisodeStatus.SCRIPT_READY
    assert len(artifacts) == 1, "Artifactが二重生成されない"
    assert len([j for j in jobs if j.type is JobType.WRITE_SCRIPT]) == 1
    # 再実行は**同じJob行を再利用する**（工程は1つ）。成功したJobを skipped へ
    # 書き換えるのは履歴の破壊なので、succeeded のままが正しい。
    # 「呼ばずに再利用した」ことの証拠は result.reused_existing_artifact 側に出る。
    assert jobs[0].status is JobStatus.SUCCEEDED


async def test_rerun_does_not_create_a_second_reservation(harness) -> None:
    """呼ばないのだから予約も増えない。"""
    generator = FakeStoryGenerator(output=good_script())
    await harness.run(generator)
    await harness.run(generator)
    assert await harness.reservations() == []
