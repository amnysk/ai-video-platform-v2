"""StoryboardActivities を Activity 単位で検査する（ADR-0013 / ADR-0015）。

Temporal を介さず直接呼ぶ。SQLite + InMemoryArtifactStore + FakeStoryboardGenerator（INV-18）。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from contracts.artifacts import StoryboardArtifact, parse_artifact
from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    ProviderCall,
    ReservationStatus,
)
from domain.artifact.hashing import canonical_json_bytes, sha256_hex
from domain.episode.transitions import EpisodeEvent
from domain.errors import (
    ArtifactConflictError,
    StoryboardInputInvalidError,
    StoryboardInputMissingError,
    StoryboardOutputUnparseableError,
    StoryboardSchemaViolationError,
    UnreconciledReservationError,
    WorkspaceUnavailableError,
    classify_failure,
)
from domain.storyboard.identity import idempotency_key, storyboard_input_hash
from domain.storyboard.ports import StoryboardRawResult, StoryboardRequest
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    JobRepository,
    ProviderReservationRepository,
)
from infrastructure.storage.artifact_store import PutResult
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
)
from workers.storyboard.activities import (
    AdmitRequest,
    CreateJobRequest,
    GenerateStoryboardRequest,
    MarkReadyRequest,
    RecordFailureRequest,
    StoryboardActivities,
    admission_token,
)

RUN_ID = "run-1"
WORKFLOW_ID = "episode-test-storyboard"


def make_activities(session_factory, store, generator) -> StoryboardActivities:
    return StoryboardActivities(
        session_factory=session_factory,
        store=store,
        generator=generator,
        bucket=BUCKET,
        timeout_seconds=5,
        prompt_template_id=PROMPT_ID,
        prompt_template_version=PROMPT_VERSION,
        model_label="label-model",
    )


async def admitted_job(activities: StoryboardActivities, episode_id: str) -> str:
    admit = await activities.admit_episode(
        AdmitRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )
    assert admit.admitted
    return await activities.create_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))


async def expected_input_hash(session_factory, episode_id: str, generator) -> str:
    async with session_factory() as session:
        script = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
    assert script is not None
    return storyboard_input_hash(
        episode_id=episode_id,
        artifact_type=ArtifactType.STORYBOARD.value,
        target_schema_version="1.0",
        script_sha256=script.sha256,
        prompt_template_id=PROMPT_ID,
        prompt_template_version=PROMPT_VERSION,
        generator_id=generator.generator_id,
        generation_spec_id=generator.generation_spec_id,
    )


async def job_status(session_factory, job_id: str) -> JobStatus:
    async with session_factory() as session:
        job = await JobRepository(session).get(job_id)
    assert job is not None
    return job.status


# ------------------------------------------------------------------ admit / job


async def test_admit_moves_script_ready_to_in_progress_and_is_idempotent(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())

    first = await activities.admit_episode(
        AdmitRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )
    again = await activities.admit_episode(
        AdmitRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )

    assert first.admitted and first.status == EpisodeStatus.IN_PROGRESS.value
    assert again.admitted and again.status == EpisodeStatus.IN_PROGRESS.value


async def test_admit_refuses_an_episode_in_progress_under_another_workflow(
    session_factory, artifact_store
) -> None:
    """台本工程が走っている in_progress の Episode に storyboard が割り込まない。

    割り込むと台本が未完成のまま needs_input で落ち、台本工程の Episode を blocked にする。
    """
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        await episodes.apply_event(episode.id, EpisodeEvent.WORKFLOW_STARTED)
        await episodes.set_workflow_id(episode.id, f"episode-{episode.id}")
        await session.commit()
    generator = FakeStoryboardGenerator()
    activities = make_activities(session_factory, artifact_store, generator)

    result = await activities.admit_episode(
        AdmitRequest(
            episode_id=episode.id, workflow_id=f"episode-{episode.id}-storyboard", run_id=RUN_ID
        )
    )

    assert result.admitted is False
    assert result.status == EpisodeStatus.IN_PROGRESS.value
    async with session_factory() as session:
        assert await JobRepository(session).list_for_episode(episode.id) == []
        still = await EpisodeRepository(session).get(episode.id)
    assert still is not None and still.status is EpisodeStatus.IN_PROGRESS
    assert generator.calls == 0


@pytest.mark.parametrize("to_blocked", [False, True])
async def test_admit_refuses_episodes_outside_the_parking_points(
    session_factory, artifact_store, to_blocked: bool
) -> None:
    async with session_factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="x")
        if to_blocked:
            await episodes.apply_event(episode.id, EpisodeEvent.WORKFLOW_STARTED)
            await episodes.apply_event(episode.id, EpisodeEvent.NEEDS_INPUT_FAILURE)
        await session.commit()
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())

    result = await activities.admit_episode(
        AdmitRequest(episode_id=episode.id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )

    assert result.admitted is False
    expected = EpisodeStatus.BLOCKED if to_blocked else EpisodeStatus.PLANNED
    assert result.status == expected.value
    async with session_factory() as session:
        assert await JobRepository(session).list_for_episode(episode.id) == []


async def test_create_job_reuses_a_non_terminal_job(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())
    request = CreateJobRequest(episode_id=episode_id, max_attempts=3)
    assert await activities.create_job(request) == await activities.create_job(request)


# ------------------------------------------------------------------ 生成


async def test_generate_stores_a_validated_storyboard_with_system_fields(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output=good_storyboard(), model="gen-model")
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    result = await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    assert generator.calls == 1 and result.reused is False
    stored = await artifact_store.get_json(result.object_key)
    assert sha256_hex(canonical_json_bytes(stored)) == result.sha256
    artifact = parse_artifact(stored)
    assert isinstance(artifact, StoryboardArtifact)
    async with session_factory() as session:
        repo = ArtifactMetadataRepository(session)
        script = await repo.find_current_by_type(episode_id, ArtifactType.SCRIPT)
        board = await repo.find_current_by_type(episode_id, ArtifactType.STORYBOARD)
    assert script is not None and board is not None
    assert artifact.source_script.artifact_id == script.id
    assert artifact.source_script.sha256 == script.sha256
    assert artifact.total_duration_ms == 25000
    assert [s.scene_id for s in artifact.scenes] == ["sb1", "sb2", "sb3"]
    assert artifact.metadata.generator == generator.generator_id
    assert artifact.metadata.generator_model == "label-model", "設定ラベル。生成器の報告値ではない"
    assert artifact.metadata.generation_spec_id == generator.generation_spec_id
    (row,) = await artifact_rows(session_factory, episode_id, ArtifactType.STORYBOARD)
    assert row.input_hash == await expected_input_hash(session_factory, episode_id, generator)
    assert await job_status(session_factory, job_id) is JobStatus.SUCCEEDED


async def test_same_input_skips_without_calling_the_generator(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    first = await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    second_job = await activities.create_job(
        CreateJobRequest(episode_id=episode_id, max_attempts=3)
    )
    assert second_job != job_id, "終端 job は再利用しない"
    again = await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=second_job, round=1)
    )

    assert generator.calls == 1
    assert again.reused is True and again.sha256 == first.sha256
    assert await job_status(session_factory, second_job) is JobStatus.SKIPPED


class _OrderCheckingGenerator(FakeStoryboardGenerator):
    """呼ばれた瞬間に、予約と dispatch が別セッションから見える（commit 済み）かを記録する。"""

    def __init__(self, session_factory, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._session_factory = session_factory
        self.seen: list[tuple[ReservationStatus, bool]] = []

    async def generate(self, request: StoryboardRequest) -> StoryboardRawResult:
        async with self._session_factory() as session:
            rows = await ProviderReservationRepository(session).find_unreconciled(
                episode_id=request.episode_id, provider=ProviderCall.CODEX_STORYBOARD
            )
        self.seen = [(row.status, row.dispatched_at is not None) for row in rows]
        return await super().generate(request)


async def test_reservation_and_dispatch_are_committed_before_the_paid_call(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = _OrderCheckingGenerator(session_factory, output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    assert generator.seen == [(ReservationStatus.RESERVED, True)]
    async with session_factory() as session:
        assert (
            await ProviderReservationRepository(session).find_unreconciled(
                episode_id=episode_id, provider=ProviderCall.CODEX_STORYBOARD
            )
            == []
        )


async def test_invalid_output_is_retryable_and_the_next_round_uses_a_new_key(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    outputs = iter([UNCOVERED_STORYBOARD, good_storyboard()])
    generator = FakeStoryboardGenerator(output=lambda _r: next(outputs))
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(StoryboardSchemaViolationError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )
    assert await job_status(session_factory, job_id) is JobStatus.RETRYABLE_FAILED

    result = await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=2)
    )
    assert generator.calls == 2 and result.reused is False

    input_hash = await expected_input_hash(session_factory, episode_id, generator)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        keys = [
            idempotency_key(
                provider=ProviderCall.CODEX_STORYBOARD.value, input_hash=input_hash, round=n
            )
            for n in (1, 2)
        ]
        r1, r2 = [await repo.find_by_key(k) for k in keys]
    assert keys[0] != keys[1]
    assert r1 is not None and r1.status is ReservationStatus.SPENT and r1.raw_output_key
    assert r1.outcome_artifact_id is None
    assert r2 is not None and r2.outcome_artifact_id == result.artifact_id


async def test_unparseable_output_is_retryable(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output="storyboard はありません")
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    with pytest.raises(StoryboardOutputUnparseableError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )


async def _reserve(session_factory, episode_id, job_id, generator, *, round: int = 1):
    input_hash = await expected_input_hash(session_factory, episode_id, generator)
    key = idempotency_key(
        provider=ProviderCall.CODEX_STORYBOARD.value, input_hash=input_hash, round=round
    )
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        reservation = await repo.reserve(
            episode_id=episode_id,
            job_id=job_id,
            provider=ProviderCall.CODEX_STORYBOARD,
            idempotency_key=key,
            input_hash=input_hash,
            round=round,
        )
        await repo.mark_dispatched(reservation.id)
        await session.commit()
    return reservation


async def test_resume_from_stored_raw_output_does_not_call_the_generator_again(
    session_factory, artifact_store
) -> None:
    """ADR-0013: 呼び出し後（spent + 生出力）に落ちていたら、生出力から解釈だけやり直す。"""
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output="never used")
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    reservation = await _reserve(session_factory, episode_id, job_id, generator)
    raw_key = f"provider-raw/{episode_id}/{reservation.id}.txt"
    await artifact_store.put_text(raw_key, good_storyboard())
    async with session_factory() as session:
        await ProviderReservationRepository(session).mark_spent(
            reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
        )
        await session.commit()

    result = await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    assert generator.calls == 0
    assert generator.interpret_calls == 1
    artifact = parse_artifact(await artifact_store.get_json(result.object_key))
    assert isinstance(artifact, StoryboardArtifact)
    assert artifact.metadata.generator_model == "label-model"


async def test_raw_saved_but_not_yet_spent_is_reconciled_by_evidence(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output="never used")
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    reservation = await _reserve(session_factory, episode_id, job_id, generator)
    await artifact_store.put_text(
        f"provider-raw/{episode_id}/{reservation.id}.txt", good_storyboard()
    )

    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    assert generator.calls == 0
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).find_by_key(reservation.idempotency_key)
    assert row is not None and row.status is ReservationStatus.SPENT
    assert row.reconciled_by == "evidence"


async def test_dispatched_without_evidence_blocks_and_never_calls(
    session_factory, artifact_store
) -> None:
    """ADR-0013 の曖昧行: 呼ばない・消さない・解放しない。"""
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    reservation = await _reserve(session_factory, episode_id, job_id, generator)

    with pytest.raises(UnreconciledReservationError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )
    # 別の起動（新しい job・別ラウンド）も塞がれる
    next_job = await activities.create_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    with pytest.raises(UnreconciledReservationError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=next_job, round=2)
        )

    assert generator.calls == 0
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).find_by_key(reservation.idempotency_key)
    assert row is not None and row.status is ReservationStatus.RESERVED


async def test_missing_script_is_needs_input_and_fails_the_job(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(
        session_factory, artifact_store, with_script=False
    )
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(StoryboardInputMissingError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )
    assert generator.calls == 0
    assert await job_status(session_factory, job_id) is JobStatus.TERMINAL_FAILED


class _CorruptingStore(InMemoryArtifactStore):
    """指定した接頭辞の JSON を読み戻したときだけ中身を変える。"""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    async def get_json(self, key: str) -> dict[str, Any]:
        payload = await super().get_json(key)
        if self._prefix in key:
            payload = {**payload, "tampered": True}
        return payload


async def test_script_sha_mismatch_is_input_invalid(session_factory) -> None:
    store = _CorruptingStore("/script/")
    episode_id = await create_episode_at_script_ready(session_factory, store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(StoryboardInputInvalidError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )
    assert generator.calls == 0


async def test_storyboard_readback_mismatch_is_not_recorded(session_factory) -> None:
    store = _CorruptingStore("/storyboard/")
    episode_id = await create_episode_at_script_ready(session_factory, store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(ArtifactConflictError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )
    async with session_factory() as session:
        board = await ArtifactMetadataRepository(session).find_current_by_type(
            episode_id, ArtifactType.STORYBOARD
        )
    assert board is None
    assert await job_status(session_factory, job_id) is JobStatus.TERMINAL_FAILED


# ------------------------------------------------------------------ prepare / release


async def _all_reservations(session_factory, episode_id: str) -> list[Any]:
    async with session_factory() as session:
        rows = await session.scalars(
            select(ProviderReservationRow).where(
                ProviderReservationRow.episode_id == uuid_of(episode_id)
            )
        )
        return list(rows)


def uuid_of(value: str) -> uuid.UUID:
    return uuid.UUID(value)


async def test_workspace_failure_in_prepare_creates_no_reservation(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(
        output=good_storyboard(), prepare_error=WorkspaceUnavailableError("disk gone")
    )
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(WorkspaceUnavailableError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )

    assert generator.calls == 0
    assert await _all_reservations(session_factory, episode_id) == []
    assert await job_status(session_factory, job_id) is JobStatus.RETRYABLE_FAILED
    assert generator.release_calls == 1, "外部呼び出し前なので解放してよい"


async def test_invalid_input_in_prepare_is_needs_input_without_reservation(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(
        output=good_storyboard(), prepare_error=StoryboardInputInvalidError("bad script")
    )
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    with pytest.raises(StoryboardInputInvalidError) as excinfo:
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )

    assert classify_failure(excinfo.value) is FailureClass.NEEDS_INPUT
    assert generator.calls == 0
    assert await _all_reservations(session_factory, episode_id) == []
    assert await job_status(session_factory, job_id) is JobStatus.TERMINAL_FAILED


class _FailingRawStore(InMemoryArtifactStore):
    async def put_text(self, key: str, body: str) -> PutResult:
        raise OSError("minio down")


async def test_raw_store_failure_after_generate_keeps_the_work_directory(
    session_factory, caplog
) -> None:
    store = _FailingRawStore()
    episode_id = await create_episode_at_script_ready(session_factory, store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, store, generator)
    job_id = await admitted_job(activities, episode_id)

    with caplog.at_level(logging.WARNING), pytest.raises(OSError):
        await activities.generate_storyboard(
            GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
        )

    assert generator.calls == 1
    assert generator.release_calls == 0, "作業領域が有料出力の唯一の写し"
    assert any("keeping storyboard work directory" in r.getMessage() for r in caplog.records)


async def test_success_releases_exactly_once(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    generator = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)

    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    assert (generator.prepare_calls, generator.calls, generator.release_calls) == (1, 1, 1)


async def test_prepare_runs_before_the_reservation(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    seen: list[int] = []

    class _Probe(FakeStoryboardGenerator):
        async def prepare(self, request: StoryboardRequest) -> None:
            seen.append(len(await _all_reservations(session_factory, request.episode_id)))
            await super().prepare(request)

    generator = _Probe(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, generator)
    job_id = await admitted_job(activities, episode_id)
    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )
    assert seen == [0]


async def test_skip_path_neither_prepares_nor_generates(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    first = FakeStoryboardGenerator(output=good_storyboard())
    activities = make_activities(session_factory, artifact_store, first)
    job_id = await admitted_job(activities, episode_id)
    await activities.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=job_id, round=1)
    )

    second = FakeStoryboardGenerator(output=good_storyboard())
    again = make_activities(session_factory, artifact_store, second)
    next_job = await again.create_job(CreateJobRequest(episode_id=episode_id, max_attempts=3))
    result = await again.generate_storyboard(
        GenerateStoryboardRequest(episode_id=episode_id, job_id=next_job, round=1)
    )

    assert result.reused is True
    assert (second.prepare_calls, second.calls, second.release_calls) == (0, 0, 0)


# ------------------------------------------------------------------ 入場トークン


async def test_same_workflow_id_with_another_run_id_is_refused(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())
    first = await activities.admit_episode(
        AdmitRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )
    stale = await activities.admit_episode(
        AdmitRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id="run-2")
    )

    assert first.admitted is True
    assert stale.admitted is False and stale.status == EpisodeStatus.IN_PROGRESS.value
    async with session_factory() as session:
        recorded = await EpisodeRepository(session).get_workflow_id(episode_id)
    assert recorded == admission_token(WORKFLOW_ID, RUN_ID)


async def test_mark_ready_from_a_stale_run_does_not_write(session_factory, artifact_store) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())
    await admitted_job(activities, episode_id)

    stale = await activities.mark_ready(
        MarkReadyRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id="old-run")
    )
    assert stale.owned is False and stale.status == EpisodeStatus.IN_PROGRESS.value

    owned = await activities.mark_ready(
        MarkReadyRequest(episode_id=episode_id, workflow_id=WORKFLOW_ID, run_id=RUN_ID)
    )
    assert owned.owned is True and owned.status == EpisodeStatus.STORYBOARD_READY.value


async def test_record_failure_from_a_stale_run_does_not_write(
    session_factory, artifact_store
) -> None:
    episode_id = await create_episode_at_script_ready(session_factory, artifact_store)
    activities = make_activities(session_factory, artifact_store, FakeStoryboardGenerator())
    job_id = await admitted_job(activities, episode_id)

    outcome = await activities.record_failure(
        RecordFailureRequest(
            episode_id=episode_id,
            job_id=job_id,
            failure_class=FailureClass.NEEDS_INPUT.value,
            error_summary="stale",
            retry_exhausted=False,
            workflow_id=WORKFLOW_ID,
            run_id="old-run",
        )
    )

    assert outcome.owned is False
    assert outcome.episode_status == EpisodeStatus.IN_PROGRESS.value
    assert await job_status(session_factory, job_id) is JobStatus.QUEUED
    async with session_factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
    assert episode is not None and episode.status is EpisodeStatus.IN_PROGRESS


# ------------------------------------------------------------------ 決定的なメタデータ


async def test_fresh_and_resumed_generation_produce_identical_canonical_bytes(
    session_factory,
) -> None:
    """generator_model は設定ラベル。生成器の報告値に依存すると再開で sha256 が変わる。"""
    fresh_store = InMemoryArtifactStore()
    fresh_episode = await create_episode_at_script_ready(session_factory, fresh_store)
    fresh_gen = FakeStoryboardGenerator(output=good_storyboard(), model="reported-model")
    fresh = make_activities(session_factory, fresh_store, fresh_gen)
    fresh_job = await admitted_job(fresh, fresh_episode)
    fresh_result = await fresh.generate_storyboard(
        GenerateStoryboardRequest(episode_id=fresh_episode, job_id=fresh_job, round=1)
    )

    resume_store = InMemoryArtifactStore()
    resume_episode = await create_episode_at_script_ready(session_factory, resume_store)
    resume_gen = FakeStoryboardGenerator(output="never used", model="other-model")
    resume = make_activities(session_factory, resume_store, resume_gen)
    resume_job = await admitted_job(resume, resume_episode)
    reservation = await _reserve(session_factory, resume_episode, resume_job, resume_gen)
    raw_key = f"provider-raw/{resume_episode}/{reservation.id}.txt"
    await resume_store.put_text(raw_key, good_storyboard())
    async with session_factory() as session:
        await ProviderReservationRepository(session).mark_spent(
            reservation.id, raw_output_key=raw_key, reconciled_by="evidence"
        )
        await session.commit()
    resume_result = await resume.generate_storyboard(
        GenerateStoryboardRequest(episode_id=resume_episode, job_id=resume_job, round=1)
    )
    assert resume_gen.calls == 0

    def _normalized(payload: dict[str, Any]) -> bytes:
        # Episode 固有の値（episode_id / 入力台本の artifact_id と sha256）だけを揃えて比べる。
        source = dict(payload["source_script"], artifact_id="-", sha256="-")
        return canonical_json_bytes({**payload, "episode_id": "-", "source_script": source})

    fresh_payload = await fresh_store.get_json(fresh_result.object_key)
    resume_payload = await resume_store.get_json(resume_result.object_key)
    assert fresh_payload["metadata"] == resume_payload["metadata"]
    assert _normalized(fresh_payload) == _normalized(resume_payload)
