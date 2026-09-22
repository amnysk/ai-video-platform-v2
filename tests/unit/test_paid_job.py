"""非同期ジョブ型の INV-15 オーケストレーション（ADR-0013 / ADR-0017 §3）。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from contracts.production_activities import AUTH_INCIDENT_SUPPRESSION_THRESHOLD
from contracts.states import ArtifactType, FailureClass, ProviderCall, ReservationStatus
from domain.errors import (
    MediaValidationError,
    ProviderCredentialSuspectedOutageError,
    ProviderJobFailedError,
    ProviderPollDeadlineError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
    UnreconciledReservationError,
)
from domain.production.ports import ImageRequest, JobFailed, JobPending, ProviderJobRef
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderAuthIncidentRepository,
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


class _SubmitCrashesAfterSend(FakeImageGenerator):
    """受理されなかったと示せない例外（例: adapter のバグ・想定外の例外）。"""

    async def submit(self, request):
        self.submit_calls += 1
        raise RuntimeError("boom after the request may have been sent")


async def test_unclassified_submit_failure_is_ambiguous_not_spent(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = _SubmitCrashesAfterSend()
    with pytest.raises(ProviderSubmitAmbiguousError):
        await runner.submit(spec, gen, REQUEST)
    async with session_factory() as session:
        row = await ProviderReservationRepository(session).find_by_key(spec.idempotency_key)
    assert row is not None and row.status is ReservationStatus.RESERVED
    assert row.dispatched_at is not None and row.provider_job_ref is None
    with pytest.raises(UnreconciledReservationError):
        await runner.submit(replace(spec, round=2), FakeImageGenerator(), REQUEST)


class _SlowDownload(FakeImageGenerator):
    async def download(self, ref, dest):
        for _ in range(10):
            await asyncio.sleep(0.02)
        await super().download(ref, dest)


async def test_keepalive_heartbeats_while_downloading_and_storing(
    session_factory, artifact_store, tmp_path
) -> None:
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
        heartbeat_interval_seconds=0.01,
    )
    spec = await _spec(session_factory)
    gen = _SlowDownload(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted)
    beats: list[object] = []
    await _await(runner, outcome.reservation_id, gen, heartbeat=beats.append)
    download_beats = [b for b in beats if isinstance(b, dict) and b.get("phase") == "download"]
    assert len(download_beats) >= 3


class _ConcurrentAttemptFinishes(FakeImageGenerator):
    """download 中に、heartbeat 切れで並行した別の await 試行が spent + Artifact まで済ませた。"""

    def __init__(self, session_factory, store, **kw) -> None:
        super().__init__(**kw)
        self.session_factory = session_factory
        self.store = store
        self.reservation_id: str | None = None
        self.episode_id: str | None = None
        self.artifact_id: str | None = None

    async def download(self, ref, dest):
        chunks: list[bytes] = []

        class _Tee:
            async def write(self, chunk: bytes) -> None:
                chunks.append(chunk)
                await dest.write(chunk)

        await super().download(ref, _Tee())
        assert self.reservation_id and self.episode_id
        key = raw_output_key(self.episode_id, self.reservation_id)
        # 同じジョブの取得物なので、並行試行の evidence は同じバイト列
        await self.store.put_bytes(key, b"".join(chunks), "application/octet-stream")
        async with self.session_factory() as session:
            await ProviderReservationRepository(session).mark_spent(
                self.reservation_id, raw_output_key=key, reconciled_by="evidence"
            )
            meta = await ArtifactMetadataRepository(session).record(
                episode_id=self.episode_id,
                artifact_type=ArtifactType.SCENE_IMAGE,
                schema_version="1",
                bucket="b",
                object_key=f"artifacts/{self.episode_id}/x.json",
                sha256="c" * 64,
                size_bytes=1,
                input_hash="h" * 64,
                scene_id="sb1",
            )
            await ProviderReservationRepository(session).attach_artifact(
                self.reservation_id, meta.id
            )
            await session.commit()
            self.artifact_id = meta.id


async def test_already_spent_with_same_evidence_is_idempotent(
    runner, session_factory, artifact_store
) -> None:
    spec = await _spec(session_factory)
    gen = _ConcurrentAttemptFinishes(session_factory, artifact_store, pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted)
    gen.reservation_id, gen.episode_id = outcome.reservation_id, spec.episode_id
    output = await _await(runner, outcome.reservation_id, gen)
    assert output.reservation.status is ReservationStatus.SPENT
    assert output.artifact is not None and output.artifact.id == gen.artifact_id


async def test_already_spent_with_different_evidence_still_refuses(runner, session_factory) -> None:
    from domain.errors import InvalidTransitionError

    class _OtherEvidence(FakeImageGenerator):
        reservation_id = ""

        async def download(self, ref, dest):
            await super().download(ref, dest)
            async with session_factory() as session:
                await ProviderReservationRepository(session).mark_spent(
                    self.reservation_id, raw_output_key=None, reconciled_by="conservative"
                )
                await session.commit()

    spec = await _spec(session_factory)
    gen = _OtherEvidence(pending_polls=0)
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted)
    gen.reservation_id = outcome.reservation_id
    with pytest.raises(InvalidTransitionError):
        await _await(runner, outcome.reservation_id, gen)


# --------------------------------------------------------------------------- 台帳から導くラウンド


async def _rounds(session_factory, spec) -> list[tuple[int, ReservationStatus]]:
    from sqlalchemy import select

    from infrastructure.db.models import ProviderReservationRow

    async with session_factory() as session:
        rows = (
            await session.scalars(
                select(ProviderReservationRow)
                .where(ProviderReservationRow.input_hash == spec.input_hash)
                .order_by(ProviderReservationRow.round)
            )
        ).all()
    return [(row.round, ReservationStatus(row.status)) for row in rows]


async def test_new_run_continues_after_rounds_consumed_by_a_previous_run(
    runner, session_factory
) -> None:
    """workflow の各 run は round=1 から数える。台帳のラウンドは台帳から導く（ADR-0017 §3）。"""
    spec = await _spec(session_factory)
    failing = _NotAccepted()
    # run 1: ラウンド 1, 2 を消費
    for attempt in (1, 2):
        with pytest.raises(ProviderJobFailedError):
            await runner.submit(replace(spec, round=attempt), failing, REQUEST)
    assert await _rounds(session_factory, spec) == [
        (1, ReservationStatus.SPENT),
        (2, ReservationStatus.SPENT),
    ]

    # run 2: 同じ入力を round=1 で頼む → 台帳ラウンド 3 を1回だけ submit
    ok = FakeImageGenerator(pending_polls=0)
    outcome = await runner.submit(replace(spec, round=1), ok, REQUEST)
    assert isinstance(outcome, Submitted) and outcome.newly_submitted and outcome.round == 3
    again = await runner.submit(replace(spec, round=1), ok, REQUEST)  # Activity 再実行
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert again.reservation_id == outcome.reservation_id
    assert ok.submit_calls == 1
    assert [r for r, _ in await _rounds(session_factory, spec)] == [1, 2, 3]
    output = await _await(runner, outcome.reservation_id, ok)
    assert output.reservation.round == 3


async def test_rejected_evidence_opens_the_next_round(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    first = await runner.submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted)
    await _await(runner, first.reservation_id, gen)
    await runner.record_output_rejected(first.reservation_id, MediaValidationError("too small"))

    row = await _reservation(session_factory, first.reservation_id)
    assert row.status is ReservationStatus.SPENT and row.failure_class is FailureClass.RETRYABLE
    second = await runner.submit(spec, gen, REQUEST)
    assert isinstance(second, Submitted) and second.newly_submitted and second.round == 2
    assert gen.submit_calls == 2


async def test_concurrent_insert_of_the_same_round_resumes_instead_of_resubmitting(
    runner, session_factory, monkeypatch
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    first = await runner.submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted)

    original = ProviderReservationRepository.find_latest_for_input
    calls = {"n": 0}

    async def stale_once(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # 別の試行が INSERT する前の古い読み取り
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ProviderReservationRepository, "find_latest_for_input", stale_once)
    again = await runner.submit(spec, gen, REQUEST)
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert again.reservation_id == first.reservation_id
    assert gen.submit_calls == 1 and calls["n"] >= 2


# --------------------------------------------------------------- ADR-0030: provider auth incidents


async def test_prepare_auth_failure_records_incident_and_creates_no_reservation(
    runner, session_factory
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(prepare_error=ProviderUnavailableError("denied", http_status=403))
    with pytest.raises(ProviderUnavailableError):
        await runner.submit(spec, gen, REQUEST)
    assert gen.submit_calls == 0
    async with session_factory() as session:
        latest = await ProviderReservationRepository(session).find_latest_for_input(
            spec.episode_id, spec.provider, spec.scene_id, spec.input_hash
        )
        assert latest is None  # 予約は一切作られない（非課金の準備は予約の前）
        count = await ProviderAuthIncidentRepository(session).count_unresolved_within_window(
            spec.provider, since=datetime.min.replace(tzinfo=UTC)
        )
        assert count == 1


async def test_repeated_auth_incidents_suppress_new_submits_for_same_provider(
    runner, session_factory
) -> None:
    for scene in ("sb1", "sb2", "sb3")[:AUTH_INCIDENT_SUPPRESSION_THRESHOLD]:
        spec = await _spec(session_factory, scene=scene)
        gen = FakeImageGenerator(prepare_error=ProviderUnavailableError("denied", http_status=403))
        with pytest.raises(ProviderUnavailableError):
            await runner.submit(spec, gen, REQUEST)

    blocked_spec = await _spec(session_factory, scene="sb4")
    blocked_gen = FakeImageGenerator()
    with pytest.raises(ProviderCredentialSuspectedOutageError):
        await runner.submit(blocked_spec, blocked_gen, REQUEST)
    assert blocked_gen.prepare_calls == 0  # 準備すら呼ばない
    assert blocked_gen.submit_calls == 0
    async with session_factory() as session:
        latest = await ProviderReservationRepository(session).find_latest_for_input(
            blocked_spec.episode_id,
            blocked_spec.provider,
            blocked_spec.scene_id,
            blocked_spec.input_hash,
        )
        assert latest is None


async def test_auth_outage_gate_is_scoped_to_one_provider(runner, session_factory) -> None:
    for scene in ("sb1", "sb2", "sb3")[:AUTH_INCIDENT_SUPPRESSION_THRESHOLD]:
        spec = await _spec(session_factory, scene=scene)
        gen = FakeImageGenerator(prepare_error=ProviderUnavailableError("denied", http_status=403))
        with pytest.raises(ProviderUnavailableError):
            await runner.submit(spec, gen, REQUEST)

    video_spec = PaidJobSpec(
        episode_id=(await _spec(session_factory, scene="sb9")).episode_id,
        scene_id="sb9",
        provider=ProviderCall.FAL_VIDEO,
        artifact_type=ArtifactType.SCENE_VIDEO,
        input_hash="v" * 64,
        round=1,
    )
    from tests.support.production import FakeVideoGenerator

    video_gen = FakeVideoGenerator(pending_polls=0)
    outcome = await runner.submit(video_spec, video_gen, REQUEST)
    assert isinstance(outcome, Submitted) and outcome.newly_submitted


async def test_successful_prepare_resolves_open_incidents(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    failing = FakeImageGenerator(prepare_error=ProviderUnavailableError("denied", http_status=403))
    with pytest.raises(ProviderUnavailableError):
        await runner.submit(spec, failing, REQUEST)

    recovered_spec = await _spec(session_factory, scene="sb2")
    healthy = FakeImageGenerator(pending_polls=0)
    outcome = await runner.submit(recovered_spec, healthy, REQUEST)
    assert isinstance(outcome, Submitted)

    async with session_factory() as session:
        count = await ProviderAuthIncidentRepository(session).count_unresolved_within_window(
            spec.provider, since=datetime.min.replace(tzinfo=UTC)
        )
        assert count == 0


async def test_auth_outage_gate_does_not_block_resuming_already_produced_scenes(
    runner, session_factory
) -> None:
    """sb1〜sb5 のように既に Artifact がある scene は、抑止期間中でも再開できる（ADR-0030）。

    ゲートは Reused / Submitted の早期returnより後（実際に provider I/O が要る場面）でだけ
    評価する。抑止中に済んだ工程の再開まで止めると failure-policy.md §4
    「済んだ工程は課金を伴わずに素通りする」が壊れる。
    """
    done_spec = await _spec(session_factory, scene="sb1")
    async with session_factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=done_spec.episode_id,
            artifact_type=ArtifactType.SCENE_IMAGE,
            schema_version="1.0",
            bucket="b",
            object_key="k",
            sha256="1" * 64,
            input_hash=done_spec.input_hash,
            scene_id="sb1",
        )
        await session.commit()

    for scene in ("sb2", "sb3", "sb4")[:AUTH_INCIDENT_SUPPRESSION_THRESHOLD]:
        spec = await _spec(session_factory, scene=scene)
        gen = FakeImageGenerator(prepare_error=ProviderUnavailableError("denied", http_status=403))
        with pytest.raises(ProviderUnavailableError):
            await runner.submit(spec, gen, REQUEST)

    # 同じ provider が抑止中でも、既に成果物がある sb1 は provider を一切呼ばずに再利用できる
    resumed = await runner.submit(done_spec, FakeImageGenerator(), REQUEST)
    assert isinstance(resumed, Reused)
