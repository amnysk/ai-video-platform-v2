"""Agent LLM の予算 gate と予約台帳（ADR-0043 §2、INV-15/44/56）。SQLite。外部呼び出しなし。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from contracts.agents import resolve_agent_spec, resolve_model_policy
from contracts.intervention import AgentRole, ControlState
from contracts.states import ProviderCall, ReservationStatus
from domain.errors import (
    AgentBudgetRefusedError,
    EpisodeHeldError,
    UnreconciledReservationError,
)
from infrastructure.agents.budget_gate import reserve_and_dispatch_agent_call, settle_agent_call
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.store import ControlChange, InterventionStore

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=UTC)
SPEC = resolve_agent_spec(AgentRole.STRATEGY, "1.0")  # 上限 0.05 USD
POLICY = resolve_model_policy(SPEC.model_policy_ref)
RATE = Decimal("150")


def _budget(**kw) -> BudgetPolicy:
    return BudgetPolicy(usd_jpy_rate=kw.pop("usd_jpy_rate", RATE), **kw)


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


async def _count(session) -> int:
    return int(await session.scalar(select(func.count()).select_from(ProviderReservationRow)) or 0)


async def _reserve(session, episode_id, *, key="agentic:e:strategy:h", budget=None):
    return await reserve_and_dispatch_agent_call(
        session,
        episode_id=episode_id,
        idempotency_key=key,
        spec=SPEC,
        policy=POLICY,
        budget_policy=budget or _budget(),
        now=NOW,
    )


async def test_the_first_call_reserves_the_ceiling_then_dispatches(session) -> None:
    episode_id = await _episode(session)
    reservation = await _reserve(session, episode_id)
    assert reservation.provider is ProviderCall.AGENT_LLM
    assert reservation.estimated_cost_usd == SPEC.max_estimated_cost_usd
    assert reservation.status is ReservationStatus.RESERVED
    assert reservation.dispatched_at is not None
    assert reservation.raw_output_key is None


async def test_without_a_usd_jpy_rate_the_call_is_refused_before_any_reservation(session) -> None:
    episode_id = await _episode(session)
    with pytest.raises(AgentBudgetRefusedError) as exc:
        await _reserve(session, episode_id, budget=_budget(usd_jpy_rate=None))
    assert exc.value.reason == "budget_unverifiable"
    assert await _count(session) == 0


async def test_exceeding_the_episode_budget_is_refused_before_any_reservation(session) -> None:
    episode_id = await _episode(session)
    # 0.05 USD * 150 = 7.5 JPY > 1 JPY
    with pytest.raises(AgentBudgetRefusedError) as exc:
        await _reserve(session, episode_id, budget=_budget(episode_budget_jpy=1))
    assert exc.value.reason == "budget_exceeded"
    assert await _count(session) == 0


async def test_earlier_ceilings_count_against_the_budget(session) -> None:
    """予約は上限（見積り）で数える: 実績が測れなくても次の予算判断に載る。"""
    episode_id = await _episode(session)
    budget = _budget(episode_budget_jpy=10)  # 7.5 JPY は 1 回目だけ収まる
    first = await _reserve(session, episode_id, key="k1", budget=budget)
    await settle_agent_call(session, first.id, raw_output_key="raw/k1")
    with pytest.raises(AgentBudgetRefusedError) as exc:
        await _reserve(session, episode_id, key="k2", budget=budget)
    assert exc.value.reason == "budget_exceeded"
    assert await _count(session) == 1


async def test_a_held_episode_refuses_dispatch_and_the_reservation_stays_undispatched(
    session,
) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    await store.lock_control(episode_id)
    await store.update_control(episode_id, ControlChange(control_state=ControlState.HELD))
    await session.commit()

    with pytest.raises(EpisodeHeldError):
        await _reserve(session, episode_id)
    await session.rollback()
    row = (await session.scalars(select(ProviderReservationRow))).one()
    assert row.status == ReservationStatus.RESERVED.value
    assert row.dispatched_at is None
    assert row.estimated_cost_usd == SPEC.max_estimated_cost_usd


async def test_a_replay_returns_the_same_reservation_without_a_second_budget_check(
    session,
) -> None:
    episode_id = await _episode(session)
    first = await _reserve(session, episode_id)
    # 換算率を外した方針でも、既存の予約は再検査せずそのまま返す
    again = await _reserve(session, episode_id, budget=_budget(usd_jpy_rate=None))
    assert again.id == first.id
    assert again.dispatched_at == first.dispatched_at
    assert await _count(session) == 1


async def test_a_held_then_resumed_replay_dispatches_the_same_reservation(session) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    await store.lock_control(episode_id)
    await store.update_control(episode_id, ControlChange(control_state=ControlState.HELD))
    await session.commit()
    with pytest.raises(EpisodeHeldError):
        await _reserve(session, episode_id)
    await session.rollback()
    await store.lock_control(episode_id)
    await store.update_control(episode_id, ControlChange(control_state=ControlState.RUNNING))
    await session.commit()

    resumed = await _reserve(session, episode_id, budget=_budget(usd_jpy_rate=None))
    assert resumed.dispatched_at is not None
    assert await _count(session) == 1


async def test_an_unreconciled_reservation_blocks_a_new_key(session) -> None:
    episode_id = await _episode(session)
    await _reserve(session, episode_id, key="k1")  # dispatched だが evidence 無し
    with pytest.raises(UnreconciledReservationError):
        await _reserve(session, episode_id, key="k2")
    assert await _count(session) == 1


async def test_settling_with_evidence_closes_the_reservation_and_unblocks_the_next(
    session,
) -> None:
    episode_id = await _episode(session)
    first = await _reserve(session, episode_id, key="k1")
    settled = await settle_agent_call(session, first.id, raw_output_key="raw/k1")
    assert settled.status is ReservationStatus.SPENT
    assert settled.raw_output_key == "raw/k1"
    assert settled.reconciled_by == "evidence"
    second = await _reserve(session, episode_id, key="k2")
    assert second.id != first.id


async def test_a_failed_call_is_settled_conservatively(session) -> None:
    episode_id = await _episode(session)
    first = await _reserve(session, episode_id)
    settled = await settle_agent_call(session, first.id, error=TimeoutError("late"))
    assert settled.status is ReservationStatus.SPENT
    assert settled.reconciled_by == "conservative"
    assert settled.raw_output_key is None
    assert settled.failure_class is not None


async def test_settling_a_success_without_evidence_is_a_programming_error(session) -> None:
    episode_id = await _episode(session)
    first = await _reserve(session, episode_id)
    with pytest.raises(ValueError):
        await settle_agent_call(session, first.id)


async def test_the_policy_must_be_the_one_the_spec_pins(session) -> None:
    episode_id = await _episode(session)
    other = resolve_model_policy("fake_llm@1")
    with pytest.raises(ValueError):
        await reserve_and_dispatch_agent_call(
            session,
            episode_id=episode_id,
            idempotency_key="k",
            spec=SPEC,
            policy=other,
            budget_policy=_budget(),
            now=NOW,
        )
    assert await _count(session) == 0
