"""PaidJobRunner の残高拒否・breaker・保留（ADR-0057 / INV-71〜74）。fake provider のみ。"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import update

from contracts.production_activities import AUTH_INCIDENT_SUPPRESSION_THRESHOLD
from contracts.provider_health import (
    BreakerState,
    HoldReason,
    ProviderAccount,
    ProviderFamily,
    RefusalKind,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall, ReservationStatus
from domain.errors import (
    ProviderBalanceExhaustedError,
    ProviderBreakerOpenError,
    ProviderRefusalUnknownError,
    ProviderSubmitAmbiguousError,
    UnreconciledReservationError,
)
from domain.production.ports import ImageRequest
from domain.provider.cost import NOT_ACCEPTED_RECONCILER, CostState, cost_state
from infrastructure.db.models import EpisodeRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderAuthIncidentRepository,
    ProviderRejectionRepository,
    ProviderReservationRepository,
)
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator

REQUEST = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")
FAL = ProviderAccount(ProviderFamily.FAL)
BALANCE = ProviderBalanceExhaustedError(
    "fal submit refused: HTTP 403 balance exhausted: Exhausted balance",
    http_status=403,
    summary="Exhausted balance",
)
UNKNOWN = ProviderRefusalUnknownError("fal submit refused: HTTP 403", http_status=403)


@pytest.fixture
def runner(session_factory, artifact_store, tmp_path) -> PaidJobRunner:
    return PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )


@pytest.fixture
def health(session_factory) -> PostgresProviderHealth:
    return PostgresProviderHealth(session_factory)


async def _spec(
    session_factory, *, scene="sb1", provider=ProviderCall.FAL_IMAGE, input_hash="h" * 64
) -> PaidJobSpec:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        # 本番では残高拒否で workflow が終わるとき Episode は blocked（保留は blocked の間だけ
        # 有効。review-E P1-1）。保留数を数えるテストが実態と同じ状態の Episode を使う
        await session.execute(
            update(EpisodeRow)
            .where(EpisodeRow.id == uuid.UUID(str(episode.id)))
            .values(status=EpisodeStatus.BLOCKED.value)
        )
        await session.commit()
    return PaidJobSpec(
        episode_id=episode.id,
        scene_id=scene,
        provider=provider,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash=input_hash,
        round=1,
        current_generation_profile_id="fake-image-profile-v1",
    )


async def _rows(session_factory, spec):
    async with session_factory() as session:
        return await ProviderReservationRepository(session).list_for_episode_provider(
            spec.episode_id, spec.provider
        )


def _state(row):
    return cost_state(
        status=row.status,
        dispatched=row.dispatched_at is not None,
        has_job_ref=row.provider_job_ref is not None,
        has_raw_output=row.raw_output_key is not None,
        reconciled_by=row.reconciled_by,
    )


async def test_balance_refusal_is_not_accepted_zero_cost_and_opens_the_breaker(
    runner, session_factory, health
) -> None:
    spec = await _spec(session_factory)
    with pytest.raises(ProviderBalanceExhaustedError):
        await runner.submit(spec, FakeImageGenerator(submit_error=BALANCE), REQUEST)

    (row,) = await _rows(session_factory, spec)
    assert row.status is ReservationStatus.SPENT
    assert row.reconciled_by == NOT_ACCEPTED_RECONCILER
    assert row.input_rejected_by_provider is False  # G2: 入力の拒否ではない
    assert _state(row) is CostState.NOT_CHARGED  # G3: 費用 0
    assert "Exhausted balance" in (row.error_summary or "")

    snap = await health.snapshot(FAL)
    assert snap.state is BreakerState.OPEN and snap.reason is RefusalKind.BALANCE_EXHAUSTED
    (hold,) = await health.list_holds()
    assert hold.episode_id == str(spec.episode_id) and hold.scene_id == "sb1"
    assert hold.reason is HoldReason.WAITING_FOR_CREDITS
    assert hold.resumable_stage == "production"
    assert hold.generation_profile_id == "fake-image-profile-v1"
    assert hold.snapshot["reservation_ids"] == [row.id]
    # G1: 認証 incident には数えない
    async with session_factory() as session:
        count = await ProviderAuthIncidentRepository(session).count_unresolved_within_window(
            ProviderCall.FAL_IMAGE, since=row.reserved_at.replace(year=2000)
        )
    assert count == 0 < AUTH_INCIDENT_SUPPRESSION_THRESHOLD


async def test_not_accepted_refusal_adds_nothing_to_the_recovery_cost(
    runner, session_factory
) -> None:
    """復旧費用の上限（INV-34）に、受理されなかった拒否の見積額を数えない。"""
    spec = await _spec(session_factory)
    with pytest.raises(ProviderBalanceExhaustedError):
        await runner.submit(spec, FakeImageGenerator(submit_error=BALANCE, cost_usd=0.5), REQUEST)
    async with session_factory() as session:
        costs = await ProviderRejectionRepository(session).list_paid_reservation_costs(
            spec.episode_id
        )
    assert [c.estimated_cost_usd for c in costs] == [0]
    # 支出は 0 でも、次の作り直しの投影に使う見積り額は残る（review-E P2-7）
    assert [c.nominal_cost_usd for c in costs] == [Decimal("0.5")]


async def test_unknown_refusal_opens_the_breaker_with_the_unknown_hold_reason(
    runner, session_factory, health
) -> None:
    spec = await _spec(session_factory)
    with pytest.raises(ProviderRefusalUnknownError):
        await runner.submit(spec, FakeImageGenerator(submit_error=UNKNOWN), REQUEST)
    snap = await health.snapshot(FAL)
    assert snap.state is BreakerState.OPEN and snap.reason is RefusalKind.UNKNOWN
    (hold,) = await health.list_holds()
    assert hold.reason is HoldReason.PROVIDER_REFUSAL_UNKNOWN


async def test_after_the_top_up_the_same_input_is_submitted_again(
    runner, session_factory, health
) -> None:
    """G2: 入金後、同じ input_hash が ProviderRejectedRetryBlockedError で恒久ブロックされない。"""
    spec = await _spec(session_factory)
    with pytest.raises(ProviderBalanceExhaustedError):
        await runner.submit(spec, FakeImageGenerator(submit_error=BALANCE), REQUEST)
    # 所有者の承認で閉じる（resume の CLI がやること）
    assert await health.close(
        FAL, approval_fingerprint="a" * 16, approved_by="owner", approval_evidence="topped up"
    )
    gen = FakeImageGenerator()
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1
    rows = await _rows(session_factory, spec)
    assert [r.round for r in rows] == [1, 2]


async def test_open_breaker_stops_before_any_reservation_for_every_fal_provider(
    runner, session_factory, health
) -> None:
    first = await _spec(session_factory)
    with pytest.raises(ProviderBalanceExhaustedError):
        await runner.submit(first, FakeImageGenerator(submit_error=BALANCE), REQUEST)

    for provider in (ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO):
        other = await _spec(session_factory, provider=provider, input_hash="g" * 64)
        gen = FakeImageGenerator()
        with pytest.raises(ProviderBreakerOpenError):
            await runner.submit(other, gen, REQUEST)
        assert gen.submit_calls == 0 and gen.prepare_calls == 0
        assert await _rows(session_factory, other) == []  # 台帳に何も残らない
    # 保留は episode ごとに記録される（first + 2 つ）
    assert await health.open_hold_count() == 3


async def test_open_breaker_does_not_block_awaiting_an_already_accepted_job(
    runner, session_factory, health
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator()
    first = await runner.submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted)
    await health.open_breaker(
        FAL,
        reason=RefusalKind.BALANCE_EXHAUSTED,
        http_status=403,
        evidence=None,
        episode_id=None,
    )
    again = await runner.submit(spec, gen, REQUEST)
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert again.reservation_id == first.reservation_id and gen.submit_calls == 1


async def test_prepare_refusal_opens_the_breaker_and_leaves_no_reservation(
    runner, session_factory, health
) -> None:
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(prepare_error=BALANCE)
    with pytest.raises(ProviderBalanceExhaustedError):
        await runner.submit(spec, gen, REQUEST)
    assert await _rows(session_factory, spec) == []
    assert (await health.snapshot(FAL)).state is BreakerState.OPEN
    assert await health.open_hold_count() == 1


async def test_accepted_submit_in_half_open_closes_the_breaker(
    runner, session_factory, health
) -> None:
    await health.open_breaker(
        FAL, reason=RefusalKind.UNKNOWN, http_status=403, evidence=None, episode_id=None
    )
    await health.half_open(
        FAL, approval_fingerprint="a" * 16, approved_by="o", approval_evidence="x"
    )
    spec = await _spec(session_factory)
    outcome = await runner.submit(spec, FakeImageGenerator(), REQUEST)
    assert isinstance(outcome, Submitted)
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED


async def test_ambiguous_submit_stays_unreconciled_and_is_never_resent(
    runner, session_factory, health
) -> None:
    """接続が送信後に切れた: 曖昧。費用は unknown、breaker は開かず、自動再送もしない。"""
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(ambiguous_submit=True)
    with pytest.raises(ProviderSubmitAmbiguousError):
        await runner.submit(spec, gen, REQUEST)
    (row,) = await _rows(session_factory, spec)
    assert row.status is ReservationStatus.RESERVED and row.dispatched_at is not None
    assert row.provider_job_ref is None
    assert _state(row) is CostState.UNKNOWN
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED

    with pytest.raises(UnreconciledReservationError):
        await runner.submit(spec, gen, REQUEST)
    assert gen.submit_calls == 1  # 再送していない
    assert len(await _rows(session_factory, spec)) == 1


async def test_non_fal_providers_are_not_gated(runner, session_factory, health) -> None:
    await health.open_breaker(
        FAL, reason=RefusalKind.BALANCE_EXHAUSTED, http_status=403, evidence=None, episode_id=None
    )
    spec = await _spec(session_factory, provider=ProviderCall.YOUTUBE_UPLOAD)
    outcome = await runner.submit(spec, FakeImageGenerator(), REQUEST)
    assert isinstance(outcome, Submitted)
