"""PaidJobRunner が月間 cap を守り、超過は保留（クラッシュではない）になる（INV-86 / ADR-0063）。

fake provider のみ。実 fal へは出ない。理由は docs/testing/fal-monthly-cap-rationale.md。
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import update

from contracts.provider_health import BreakerState, HoldReason, ProviderAccount, ProviderFamily
from contracts.states import ArtifactType, EpisodeStatus, FailureClass, ProviderCall
from domain.budget_gate import evaluate_budget
from domain.errors import (
    ProviderBalanceExhaustedError,
    ProviderBudgetHeldError,
    ProviderInputFetchError,
    ProviderMonthlyBudgetExceededError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    failure_class_from_type_name,
)
from domain.production.ports import ImageRequest
from infrastructure.db.models import EpisodeRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.log_capture import capture_json
from tests.support.monthly_cap import count_rows, reserve_capped, seed
from tests.support.production import FakeImageGenerator

D = Decimal
REQUEST = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")
FAL = ProviderAccount(ProviderFamily.FAL)


def _runner(session_factory, artifact_store, tmp_path, cap: str | None = "60") -> PaidJobRunner:
    return PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
        budget=evaluate_budget(None if cap is None else D(cap)),
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


async def _episode_rows(session_factory, spec):
    async with session_factory() as session:
        return await ProviderReservationRepository(session).list_for_episode_provider(
            spec.episode_id, spec.provider
        )


async def test_a_submit_within_the_cap_goes_through(session_factory, artifact_store, tmp_path):
    await seed(session_factory, "10")
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=5.0)
    outcome = await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1
    (row,) = await _episode_rows(session_factory, spec)
    assert row.estimated_cost_usd == D("5")


async def test_exactly_at_the_cap_goes_through(session_factory, artifact_store, tmp_path):
    await seed(session_factory, "55")
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=5.0)
    outcome = await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1


async def test_over_the_cap_holds_for_credits_with_no_row_and_no_fal_call(
    session_factory, artifact_store, tmp_path
):
    await seed(session_factory, "58")
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=5.0)
    runner = _runner(session_factory, artifact_store, tmp_path)

    with capture_json() as logs, pytest.raises(ProviderMonthlyBudgetExceededError):
        await runner.submit(spec, gen, REQUEST)

    assert gen.submit_calls == 0  # fal は呼ばない
    assert await _episode_rows(session_factory, spec) == []  # この Episode の予約行は無い
    assert await count_rows(session_factory) == 1  # 台帳全体も種まきの 1 行のまま
    health = PostgresProviderHealth(session_factory)
    (hold,) = await health.list_holds()  # クラッシュではなく既存の保留経路
    assert hold.reason is HoldReason.WAITING_FOR_CREDITS
    assert hold.snapshot["hold_detail"]["decision_reason"] == "monthly_cap_exceeded"
    assert (await health.snapshot(FAL)).state is BreakerState.CLOSED  # 残高拒否ではない
    [event] = logs.events("provider.call.suppressed")
    attrs = event["attributes"]
    assert attrs["decision_reason"] == "monthly_cap_exceeded"
    assert D(str(attrs["month_counted"])) == D("58")
    assert D(str(attrs["estimate"])) == D("5")
    assert D(str(attrs["cap"])) == D("60")
    assert D(str(attrs["remaining"])) == D("2")


async def test_the_cap_hold_is_needs_input_so_the_workflow_ends_normally() -> None:
    name = ProviderMonthlyBudgetExceededError.__name__
    assert failure_class_from_type_name(name) is FailureClass.NEEDS_INPUT


async def test_the_cap_hold_is_idempotent_per_scene_and_resumable_after_raising_the_budget(
    session_factory, artifact_store, tmp_path
):
    await seed(session_factory, "58")
    spec = await _spec(session_factory)
    for _ in range(3):
        with pytest.raises(ProviderBudgetHeldError):
            await _runner(session_factory, artifact_store, tmp_path).submit(
                spec, FakeImageGenerator(cost_usd=5.0), REQUEST
            )
    assert await PostgresProviderHealth(session_factory).open_hold_count() == 1
    # 予算を上げて worker を作り直した（ADR-0061: 起動時に 1 回だけ読む）→ 同じ spec が通る
    gen = FakeImageGenerator(cost_usd=5.0)
    outcome = await _runner(session_factory, artifact_store, tmp_path, cap="100").submit(
        spec, gen, REQUEST
    )
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1


@pytest.mark.parametrize("provider", [ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO])
async def test_the_cap_covers_the_video_path_too(
    session_factory, artifact_store, tmp_path, provider
):
    await seed(session_factory, "60", provider=ProviderCall.FAL_VIDEO)
    spec = await _spec(session_factory, provider=provider)
    gen = FakeImageGenerator(cost_usd=0.01)
    with pytest.raises(ProviderMonthlyBudgetExceededError):
        await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert gen.submit_calls == 0


class _BrokenEstimate(FakeImageGenerator):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def estimate_cost_usd(self, request):
        if self.mode == "raise":
            raise ValueError("cost model unavailable")
        return float(self.mode)  # "nan" / "inf" / "-1"


@pytest.mark.parametrize("mode", ["raise", "nan", "inf", "-1"])
async def test_an_unknown_estimate_fails_closed(session_factory, artifact_store, tmp_path, mode):
    spec = await _spec(session_factory)
    gen = _BrokenEstimate(mode)
    with pytest.raises(ProviderBudgetHeldError):
        await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert gen.submit_calls == 0
    assert await _episode_rows(session_factory, spec) == []
    (hold,) = await PostgresProviderHealth(session_factory).list_holds()
    expected = {"unknown_cost"} if mode == "raise" else {"unknown_cost", "invalid_input"}
    assert hold.snapshot["hold_detail"]["decision_reason"] in expected  # どれも fail closed


async def test_a_null_estimate_row_in_the_month_fails_closed(
    session_factory, artifact_store, tmp_path
):
    await seed(session_factory, None)
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=1.0)
    with pytest.raises(ProviderBudgetHeldError):
        await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert gen.submit_calls == 0


# ------------------------------------------------------------------ 冪等（再実行・worker 再起動）


async def test_replaying_the_same_activity_is_not_cap_denied_nor_double_counted(
    session_factory, artifact_store, tmp_path
):
    """cap ちょうどまで使った submit を、worker 再起動後 / Temporal の再実行で同じ入力に
    再実行しても cap 拒否されず、fal も再送されず、行は増えない。"""
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=60.0)
    first = await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert isinstance(first, Submitted) and first.newly_submitted
    for _ in range(2):  # 新しい runner = worker 再起動
        again = await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
        assert isinstance(again, Submitted) and not again.newly_submitted
        assert again.reservation_id == first.reservation_id
    assert gen.submit_calls == 1
    assert len(await _episode_rows(session_factory, spec)) == 1


async def test_a_crash_between_reserve_and_dispatch_resumes_without_a_cap_check(
    session_factory, artifact_store, tmp_path
):
    """reserve を commit した直後に worker が落ちた（未 dispatch）。見積は既に月に入っているので、
    再実行はその行を再開するだけで、cap（ちょうど使い切り）に拒否されない。"""
    spec = await _spec(session_factory)
    async with session_factory() as session:
        await ProviderReservationRepository(session).reserve(
            episode_id=spec.episode_id,
            provider=spec.provider,
            idempotency_key=spec.key_for_round(1),
            input_hash=spec.input_hash,
            round=1,
            scene_id=spec.scene_id,
            estimated_cost_usd=D("60"),
        )
        await session.commit()
    gen = FakeImageGenerator(cost_usd=60.0)
    outcome = await _runner(session_factory, artifact_store, tmp_path).submit(spec, gen, REQUEST)
    assert isinstance(outcome, Submitted) and gen.submit_calls == 1
    assert len(await _episode_rows(session_factory, spec)) == 1


# ------------------------------------------------------------------ 予算 0 への切替


async def test_switching_the_budget_to_zero_blocks_new_reservations_only(
    session_factory, artifact_store, tmp_path
):
    first_spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=5.0)
    submitted = await _runner(session_factory, artifact_store, tmp_path).submit(
        first_spec, gen, REQUEST
    )
    assert isinstance(submitted, Submitted)

    zero = _runner(session_factory, artifact_store, tmp_path, cap="0")
    new_spec = await _spec(session_factory)
    new_gen = FakeImageGenerator(cost_usd=1.0)
    with pytest.raises(ProviderBudgetHeldError):
        await zero.submit(new_spec, new_gen, REQUEST)
    assert new_gen.submit_calls == 0 and await _episode_rows(session_factory, new_spec) == []

    # 飛行中の予約には触らない（キャンセルも再送もしない）
    again = await zero.submit(first_spec, gen, REQUEST)
    assert isinstance(again, Submitted) and not again.newly_submitted
    (row,) = await _episode_rows(session_factory, first_spec)
    assert row.status.value == "reserved" and row.id == submitted.reservation_id


# ------------------------------------------------------------------ fal の失敗経路と計上


@pytest.mark.parametrize(
    ("error", "counted"),
    [
        (TimeoutError("fal timed out"), True),  # 曖昧: 参照なしの dispatched → 安全側で数える
        (ProviderRejectedError("fal 422 validation"), True),  # 戻った拒否 → 課金前提で spent
        (ProviderInputFetchError("fal could not fetch input (404)"), True),
        (ProviderBalanceExhaustedError("403 exhausted", http_status=403), False),  # 費用 0
    ],
    ids=["timeout", "422-rejected", "404-input-fetch", "balance-refusal"],
)
async def test_failed_submits_are_counted_or_released_per_the_ledger_semantics(
    session_factory, artifact_store, tmp_path, error, counted
):
    spec = await _spec(session_factory)
    gen = FakeImageGenerator(cost_usd=30.0, submit_error=error)
    runner = _runner(session_factory, artifact_store, tmp_path)
    with pytest.raises(Exception) as caught:
        await runner.submit(spec, gen, REQUEST)
    assert not isinstance(caught.value, ProviderMonthlyBudgetExceededError)
    assert gen.submit_calls == 1
    if isinstance(error, TimeoutError):
        assert isinstance(caught.value, ProviderSubmitAmbiguousError)
    assert len(await _episode_rows(session_factory, spec)) == 1  # 予約は消えない

    # 失敗した 30 が月に残るか: 台帳へ直接 31 を cap 60 で入れてみる（breaker の影響を除く）
    if counted:
        with pytest.raises(ProviderMonthlyBudgetExceededError):
            await reserve_capped(session_factory, "31", now=None)
    else:
        await reserve_capped(session_factory, "31", now=None)
