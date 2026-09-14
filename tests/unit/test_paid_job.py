"""非同期ジョブ型の INV-15 オーケストレーション（ADR-0013 / ADR-0017 §3）。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from decimal import Decimal

import pytest

from contracts.states import ArtifactType, FailureClass, ProviderCall, ReservationStatus
from domain.errors import (
    MediaValidationError,
    ProviderJobFailedError,
    ProviderPollDeadlineError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    UnreconciledReservationError,
)
from domain.production.ports import ImageRequest, JobFailed, JobPending, ProviderJobRef
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservation,
    ProviderReservationRepository,
)
from infrastructure.production.paid_job import (
    PaidJobRunner,
    PaidJobSpec,
    Reused,
    Submitted,
    raw_output_key,
    raw_result_key,
)
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator

REQUEST = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")


async def _noop_sleep(_: float) -> None:
    return None


@pytest.fixture
def runner(session_factory, artifact_store, tmp_path) -> PaidJobRunner:
    return PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )


async def _spec(session_factory, *, scene="sb1", round=1, input_hash="h" * 64) -> PaidJobSpec:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    return PaidJobSpec(
        episode_id=episode.id,
        scene_id=scene,
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash=input_hash,
        round=round,
    )


async def _reservation(session_factory, reservation_id) -> ProviderReservation:
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).get(reservation_id)
    assert row is not None
    return row


async def _await(runner, rid, gen, **kw):
    kw.setdefault("poll_interval_seconds", 0)
    kw.setdefault("sleep", _noop_sleep)
    return await runner.await_output(rid, gen, **kw)


async def test_happy_path_writes_ledger_in_order(runner, session_factory, artifact_store) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=2, cost_usd=0.04)
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and outcome.newly_submitted
    row = await _reservation(session_factory, outcome.reservation_id)
    assert row.status is ReservationStatus.RESERVED
    assert row.dispatched_at is not None and row.provider_job_ref
    assert row.estimated_cost_usd == Decimal("0.0400")
    assert row.scene_id == "sb1"

    beats: list[object] = []
    output = await _await(runner, outcome.reservation_id, gen, heartbeat=beats.append)
    assert gen.submit_calls == 1 and gen.poll_calls == 3 and gen.download_calls == 1
    assert len(beats) == 4  # 3 polls + 1 after download
    assert output.data[:8] == b"\x89PNG\r\n\x1a\n"
    assert output.reservation.status is ReservationStatus.SPENT
    assert output.reservation.reconciled_by == "evidence"
    assert output.raw_output_key == raw_output_key(spec.episode_id, outcome.reservation_id)
    assert await artifact_store.get_bytes(output.raw_output_key) == output.data


async def test_existing_artifact_is_reused_without_reserving(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    async with session_factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=spec.episode_id,
            artifact_type=ArtifactType.SCENE_IMAGE,
            schema_version="1.0",
            bucket="b",
            object_key="k",
            sha256="1" * 64,
            input_hash=spec.input_hash,
            scene_id="sb1",
        )
        await session.commit()
    gen = FakeImageGenerator()
    assert isinstance(await runner.submit(spec, gen, REQUEST), Reused)
    other_scene = replace(spec, scene_id="sb2")
    assert isinstance(await runner.submit(other_scene, gen, REQUEST), Submitted)
    assert gen.submit_calls == 1


async def test_crash_after_reserve_before_dispatch_continues_same_key(
    runner, session_factory
) -> None:
    spec = await _spec(session_factory)
    async with session_factory() as session:
        await ProviderReservationRepository(session).reserve(
            episode_id=spec.episode_id,
            provider=spec.provider,
            idempotency_key=spec.idempotency_key,
            input_hash=spec.input_hash,
            round=1,
            scene_id="sb1",
        )
        await session.commit()
    gen = FakeImageGenerator()
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1


async def test_crash_after_dispatch_without_ref_is_never_resent(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        row = await repo.reserve(
            episode_id=spec.episode_id,
            provider=spec.provider,
            idempotency_key=spec.idempotency_key,
            input_hash=spec.input_hash,
            round=1,
            scene_id="sb1",
        )
        await repo.mark_dispatched(row.id)
        await session.commit()
    gen = FakeImageGenerator()
    with pytest.raises(UnreconciledReservationError):
        await runner.submit(spec, gen, REQUEST)
    # 次ラウンドも同じシーンでは止まる
    with pytest.raises(UnreconciledReservationError):
        await runner.submit(replace(spec, round=2), gen, REQUEST)
    with pytest.raises(UnreconciledReservationError):
        await _await(runner, row.id, gen)
    assert gen.submit_calls == 0


async def test_resume_after_ref_awaits_without_resubmitting(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    first = await runner.submit(spec, gen, REQUEST)
    again = await runner.submit(spec, gen, REQUEST)  # submit Activity の再実行
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert again.reservation_id == first.reservation_id  # type: ignore[union-attr]
    await _await(runner, again.reservation_id, gen)
    assert gen.submit_calls == 1


async def test_resume_after_spent_before_artifact_reuses_evidence(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    first = await _await(runner, outcome.reservation_id, gen)
    again = await runner.submit(spec, gen, REQUEST)
    second = await _await(runner, again.reservation_id, gen)  # type: ignore[union-attr]
    assert second.data == first.data
    assert gen.submit_calls == 1 and gen.download_calls == 1


async def test_crash_between_raw_store_and_spent_uses_stored_evidence(
    runner, session_factory, artifact_store
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    await artifact_store.put_bytes(
        raw_output_key(spec.episode_id, outcome.reservation_id), b"raw", "x"
    )
    output = await _await(runner, outcome.reservation_id, gen)
    assert output.data == b"raw" and gen.poll_calls == 0
    assert output.reservation.status is ReservationStatus.SPENT


async def test_ambiguous_submit_leaves_dispatched_reservation(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(ambiguous_submit=True)
    with pytest.raises(ProviderSubmitAmbiguousError):
        await runner.submit(spec, gen, REQUEST)
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).find_by_key(spec.idempotency_key)
    assert row is not None and row.status is ReservationStatus.RESERVED
    assert row.dispatched_at is not None and row.provider_job_ref is None
    with pytest.raises(UnreconciledReservationError):
        await runner.submit(replace(spec, round=2), gen, REQUEST)
    assert gen.submit_calls == 1


class _NotAccepted(FakeImageGenerator):
    async def submit(self, request):
        self.submit_calls += 1
        raise ProviderJobFailedError("429")


async def test_clean_not_accepted_submit_is_conservatively_spent(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = _NotAccepted()
    with pytest.raises(ProviderJobFailedError):
        await runner.submit(spec, gen, REQUEST)
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).find_by_key(spec.idempotency_key)
    assert row is not None and row.status is ReservationStatus.SPENT
    assert row.reconciled_by == "conservative" and row.failure_class is FailureClass.RETRYABLE
    # 同じラウンドは再送しない、次ラウンドは進める
    with pytest.raises(ProviderJobFailedError):
        await _await(runner, row.id, gen)
    ok = FakeImageGenerator()
    assert isinstance(await runner.submit(replace(spec, round=2), ok, REQUEST), Submitted)
    assert gen.submit_calls == 1


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (JobFailed("policy", rejected=True), ProviderRejectedError),
        (JobFailed("runner"), ProviderJobFailedError),
    ],
)
async def test_job_failure_spends_conservatively(runner, session_factory, failure, error) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0, fail_with=failure)
    outcome = await runner.submit(spec, gen, REQUEST)
    with pytest.raises(error):
        await _await(runner, outcome.reservation_id, gen)
    row = await _reservation(session_factory, outcome.reservation_id)
    assert row.status is ReservationStatus.SPENT and row.reconciled_by == "conservative"
    assert gen.download_calls == 0


async def test_download_over_cap_is_spent_and_not_stored(
    runner, session_factory, artifact_store
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    with pytest.raises(MediaValidationError):
        await _await(runner, outcome.reservation_id, gen, max_bytes=10)
    row = await _reservation(session_factory, outcome.reservation_id)
    assert row.status is ReservationStatus.SPENT and row.raw_output_key is None
    assert not await artifact_store.exists(raw_output_key(spec.episode_id, outcome.reservation_id))


async def test_poll_deadline_keeps_the_ref_for_a_later_await(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=10_000)
    outcome = await runner.submit(spec, gen, REQUEST)
    ticks = iter(range(100))
    with pytest.raises(ProviderPollDeadlineError):
        await _await(
            runner, outcome.reservation_id, gen, deadline_seconds=3, clock=lambda: next(ticks)
        )
    row = await _reservation(session_factory, outcome.reservation_id)
    assert row.status is ReservationStatus.RESERVED and row.provider_job_ref


async def test_cancellation_stops_polling_and_leaves_ledger(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=10_000)
    outcome = await runner.submit(spec, gen, REQUEST)
    beats: list[object] = []
    task = asyncio.create_task(
        runner.await_output(
            outcome.reservation_id, gen, poll_interval_seconds=0.01, heartbeat=beats.append
        )
    )
    while len(beats) < 3:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    polls = gen.poll_calls
    await asyncio.sleep(0.05)
    assert gen.poll_calls == polls
    row = await _reservation(session_factory, outcome.reservation_id)
    assert row.status is ReservationStatus.RESERVED and row.provider_job_ref


class _Describing(FakeImageGenerator):
    async def describe_result(self, ref: ProviderJobRef) -> dict:
        return {"request_id": str(ref), "seed": 3}


async def test_result_json_is_stored_as_evidence(runner, session_factory, artifact_store) -> None:
    spec = await _spec(session_factory)
    gen = _Describing(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    await _await(runner, outcome.reservation_id, gen)
    assert '"seed": 3' in await artifact_store.get_text(
        raw_result_key(spec.episode_id, outcome.reservation_id)
    )


async def test_unknown_reservation(runner) -> None:
    with pytest.raises(UnreconciledReservationError):
        await _await(runner, str(uuid.uuid4()), FakeImageGenerator())


def test_pending_status_type_is_shared() -> None:
    assert isinstance(JobPending(), JobPending)
