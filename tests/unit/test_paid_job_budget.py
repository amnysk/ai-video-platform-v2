"""PaidJobRunner の予算ゲート（ADR-0061 / INV-84）。fake provider のみ・実 fal へは出ない。

予算 0 / 未設定では、予約も prepare も submit も起きない。済んだ工程の再利用と
未照合予約の扱い（INV-17 / INV-67）は予算に関わらず変わらない。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import update

from contracts.provider_health import BreakerState, HoldReason, ProviderAccount, ProviderFamily
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.budget_gate import evaluate_budget
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    ProviderBudgetHeldError,
    UnreconciledReservationError,
)
from domain.production.ports import ImageRequest
from infrastructure.db.models import EpisodeRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator

REQUEST = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")
FAL = ProviderAccount(ProviderFamily.FAL)


def _runner(session_factory, artifact_store, tmp_path, budget) -> PaidJobRunner:
    return PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
        budget=budget,
    )


async def _spec(session_factory, *, provider=ProviderCall.FAL_IMAGE, scene="sb1") -> PaidJobSpec:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
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
        input_hash="h" * 64,
        round=1,
        current_generation_profile_id="fake-image-profile-v1",
    )


@pytest.mark.parametrize(
    ("budget_usd", "status", "decision"),
    [
        (Decimal("0"), "disabled", "budget_disabled"),
        (None, "unset", "budget_unset"),
        (Decimal("-3"), "unset", "budget_invalid"),
    ],
)
@pytest.mark.parametrize("provider", [ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO])
async def test_zero_or_unset_budget_never_reserves_prepares_or_submits(
    session_factory, artifact_store, tmp_path, budget_usd, status, decision, provider
) -> None:
    runner = _runner(session_factory, artifact_store, tmp_path, evaluate_budget(budget_usd))
    spec = await _spec(session_factory, provider=provider)
    gen = FakeImageGenerator()

    with pytest.raises(ProviderBudgetHeldError):
        await runner.submit(spec, gen, REQUEST)

    assert gen.submit_calls == 0 and gen.prepare_calls == 0
    async with session_factory() as session:
        rows = await ProviderReservationRepository(session).list_for_episode_provider(
            spec.episode_id, spec.provider
        )
    assert rows == []  # 台帳に何も残らない
    health = PostgresProviderHealth(session_factory)
    (hold,) = await health.list_holds()
    assert hold.reason is HoldReason.WAITING_FOR_CREDITS
    # 機械判定可能な値が保留の行に残る（reason の CHECK は広げない / ADR-0061）
    assert hold.snapshot["hold_detail"] == {"budget_status": status, "decision_reason": decision}
    # 予算は breaker を開かない（残高拒否ではない）。予算を足せば再開の関門は resume だけ
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED


async def test_budget_held_error_is_needs_input_so_the_workflow_ends_normally() -> None:
    from contracts.states import FailureClass

    assert FAILURE_CLASS_BY_TYPE_NAME["ProviderBudgetHeldError"] is FailureClass.NEEDS_INPUT


async def test_budget_hold_is_idempotent_per_scene(
    session_factory, artifact_store, tmp_path
) -> None:
    runner = _runner(session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("0")))
    spec = await _spec(session_factory)
    for _ in range(3):
        with pytest.raises(ProviderBudgetHeldError):
            await runner.submit(spec, FakeImageGenerator(), REQUEST)
    assert await PostgresProviderHealth(session_factory).open_hold_count() == 1


async def test_positive_budget_submits_normally(session_factory, artifact_store, tmp_path) -> None:
    runner = _runner(session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("10")))
    spec = await _spec(session_factory)
    gen = FakeImageGenerator()
    outcome = await runner.submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1


async def test_already_accepted_job_is_not_resubmitted_when_budget_drops_to_zero(
    session_factory, artifact_store, tmp_path
) -> None:
    """受理済みジョブの再入は新規 submit ではない（二重課金の防止は従来どおり）。"""
    spec = await _spec(session_factory)
    gen = FakeImageGenerator()
    first = await _runner(
        session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("10"))
    ).submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted) and gen.submit_calls == 1

    again = await _runner(
        session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("0"))
    ).submit(spec, gen, REQUEST)
    assert isinstance(again, Submitted) and not again.newly_submitted
    assert gen.submit_calls == 1  # 予算 0 でも、再 submit は 0 回


async def test_unreconciled_reservation_is_never_resent_after_budget_is_added(
    session_factory, artifact_store, tmp_path
) -> None:
    """予算が正になっただけで、未照合予約は再送されない（INV-67）。"""
    spec = await _spec(session_factory)
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        row = await repo.reserve(
            episode_id=spec.episode_id,
            provider=spec.provider,
            idempotency_key="other-key",
            input_hash=spec.input_hash,
            round=1,
            scene_id=spec.scene_id,
        )
        await repo.mark_dispatched(row.id)
        await session.commit()
    runner = _runner(session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("100")))
    gen = FakeImageGenerator()
    with pytest.raises(UnreconciledReservationError):
        await runner.submit(spec, gen, REQUEST)
    assert gen.submit_calls == 0


async def test_a_broken_log_pipeline_does_not_change_the_held_outcome(
    session_factory, artifact_store, tmp_path, monkeypatch
) -> None:
    """ログ基盤（OpenSearch）の故障は業務へ伝播しない（INV-38）。保留の型も fal 0 回も同じ。"""
    import logging

    class _Exploding(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise OSError("log sink is down")

    handler = _Exploding(level=logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    try:
        runner = _runner(session_factory, artifact_store, tmp_path, evaluate_budget(Decimal("0")))
        spec = await _spec(session_factory)
        gen = FakeImageGenerator()
        with pytest.raises(ProviderBudgetHeldError):
            await runner.submit(spec, gen, REQUEST)
        assert gen.submit_calls == 0
    finally:
        root.removeHandler(handler)


async def test_runner_built_without_a_budget_fails_closed(
    session_factory, artifact_store, tmp_path
) -> None:
    """結線し忘れ（budget 省略 / None）は「未設定」として止まる。fail-open にしない。"""
    workdir = WorkDirectory(tmp_path / "work", forbidden=())
    for runner in (
        PaidJobRunner(session_factory=session_factory, store=artifact_store, workdir=workdir),
        PaidJobRunner(
            session_factory=session_factory, store=artifact_store, workdir=workdir, budget=None
        ),
    ):
        spec = await _spec(session_factory)
        gen = FakeImageGenerator()

        with pytest.raises(ProviderBudgetHeldError):
            await runner.submit(spec, gen, REQUEST)

        assert gen.submit_calls == 0 and gen.prepare_calls == 0
