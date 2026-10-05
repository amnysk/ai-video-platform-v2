"""有料ジョブの経過がログで辿れること（log-contract §3・§6・§9）。

理由は docs/testing/logging-rationale.md。

台帳の正は DB。ここは「ログの順序と照合用 ID が DB と食い違わない」ことを見る。
"""

from __future__ import annotations

import pytest

from contracts.states import ArtifactType, ProviderCall
from domain.errors import ProviderJobFailedError, UnreconciledReservationError
from domain.production.ports import ImageRequest, JobFailed
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.log_capture import capture_json
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


async def _spec(session_factory, *, run_attempt: int = 1) -> PaidJobSpec:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    return PaidJobSpec(
        episode_id=episode.id,
        scene_id="sb6",
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash="h" * 64,
        round=run_attempt,
    )


async def test_a_successful_round_reads_in_ledger_order(runner, session_factory) -> None:
    spec = await _spec(session_factory, run_attempt=3)
    gen = FakeImageGenerator(pending_polls=2)
    with capture_json() as logs:
        submitted = await runner.submit(spec, gen, REQUEST)
        assert isinstance(submitted, Submitted)
        await runner.await_output(
            submitted.reservation_id, gen, poll_interval_seconds=0, sleep=_noop_sleep
        )
    names = logs.names()
    assert names == [
        "reservation.reserved",
        "reservation.dispatched",
        "provider.call.succeeded",  # ref の commit の前（§9 の例外）
        "reservation.job_ref_recorded",
        "provider.job.state_changed",  # pending（この試行の最初の観測）
        "provider.job.state_changed",  # succeeded
        "reservation.spent",
    ]
    for event in logs.events():
        if event["event_name"] == "log.record":
            continue
        assert event["episode_id"] == spec.episode_id
        assert event["scene_id"] == "sb6"
        assert event["provider"] == "fal_image"
    accepted = logs.events("provider.call.succeeded")[0]
    assert accepted["provider_operation"] == "submit"
    assert accepted["reservation_id"] == submitted.reservation_id
    assert accepted["provider_attempt"] == 1  # 台帳ラウンド
    assert accepted["attributes"]["run_attempt"] == 3  # run ごとの試行番号は attributes
    polls = logs.events("provider.job.state_changed")
    assert [p["attributes"]["state"] for p in polls] == ["pending", "succeeded"]


async def test_resume_and_reuse_are_visible(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0)
    first = await runner.submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted)
    with capture_json() as logs:
        again = await runner.submit(spec, gen, REQUEST)
    assert isinstance(again, Submitted) and again.reservation_id == first.reservation_id
    [resumed] = logs.events("reservation.resumed")
    assert resumed["reservation_id"] == first.reservation_id
    assert resumed["attributes"]["phase"] == "await"


async def test_failed_job_spends_conservatively_and_is_logged(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(pending_polls=0, fail_with=JobFailed(message="runner died"))
    submitted = await runner.submit(spec, gen, REQUEST)
    assert isinstance(submitted, Submitted)
    with capture_json() as logs, pytest.raises(ProviderJobFailedError):
        await runner.await_output(
            submitted.reservation_id, gen, poll_interval_seconds=0, sleep=_noop_sleep
        )
    [spent] = logs.events("reservation.spent")
    assert spent["attributes"]["reconciled_by"] == "conservative"
    assert [p["attributes"]["state"] for p in logs.events("provider.job.state_changed")] == [
        "failed"
    ]


async def test_an_unreconciled_reservation_blocks_and_is_logged(runner, session_factory) -> None:
    spec = await _spec(session_factory)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        stuck = await repo.reserve(
            episode_id=spec.episode_id,
            provider=spec.provider,
            idempotency_key=spec.key_for_round(1),
            input_hash=spec.input_hash,
            round=1,
            scene_id=spec.scene_id,
        )
        await repo.mark_dispatched(stuck.id)
        await session.commit()
    with capture_json() as logs, pytest.raises(UnreconciledReservationError):
        await runner.submit(spec, FakeImageGenerator(), REQUEST)
    [blocked] = logs.events("reservation.blocked")
    assert blocked["reservation_id"] == stuck.id
    assert blocked["error_category"] == "unreconciled_reservation"
