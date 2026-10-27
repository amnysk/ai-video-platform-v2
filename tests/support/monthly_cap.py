"""月間 fal 予算の hard cap（INV-86 / ADR-0063）のテストが共有する台帳の種まき。

規則（何を数えるか）はここに複製しない。種まきは「各費用区分の行を作る」だけで、期待値は
各テストが明示する（``domain/provider/cost.py`` の区分を import して使う）。
理由は docs/testing/fal-monthly-cap-rationale.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.states import ProviderCall
from domain.provider.cost import NOT_ACCEPTED_RECONCILER, CostState
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository

#: 判定に渡す「今」。月の途中（UTC）に固定して、実時計の月替わりで不安定にならないようにする
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
#: 種まきした行の既定の reserved_at（NOW と同じ月）
SEED_AT = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
CAP = Decimal("60")

#: 費用区分 → その区分の行を作る手順。区分の導出は ``cost_state`` が行うので、
#: 種まきの正しさは ``test_seeded_rows_have_the_intended_cost_state`` が確かめる
SEEDABLE_STATES: tuple[str, ...] = (
    CostState.ESTIMATED.value,
    CostState.PROVIDER_ACCEPTED.value,
    CostState.CONFIRMED_CHARGED.value,
    CostState.NOT_CHARGED.value,
    CostState.ASSUMED_CHARGED.value,
    CostState.UNKNOWN.value,
    "abandoned",
)


async def new_episode(session_factory: async_sessionmaker[AsyncSession]) -> str:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
        return str(episode.id)


async def seed(
    session_factory: async_sessionmaker[AsyncSession],
    estimate: Decimal | str | None,
    state: str = CostState.CONFIRMED_CHARGED.value,
    *,
    provider: ProviderCall = ProviderCall.FAL_IMAGE,
    reserved_at: datetime = SEED_AT,
    episode_id: str | None = None,
) -> str:
    """指定の費用区分の予約行を 1 件作る（cap 無しの reserve。back-compat 経路）。"""
    episode_id = episode_id or await new_episode(session_factory)
    est = None if estimate is None else Decimal(str(estimate))
    key = f"seed-{uuid.uuid4().hex}"
    async with session_factory() as session:
        repo = ProviderReservationRepository(session)
        row = await repo.reserve(
            episode_id=episode_id,
            provider=provider,
            idempotency_key=key,
            input_hash=uuid.uuid4().hex * 2,
            round=1,
            scene_id="sb1",
            estimated_cost_usd=est,
        )
        rid = row.id
        if state == CostState.ESTIMATED.value:
            pass
        elif state == CostState.PROVIDER_ACCEPTED.value:
            await repo.mark_dispatched(rid)
            await repo.record_provider_job_ref(rid, "ref-1")
        elif state == CostState.UNKNOWN.value:
            await repo.mark_dispatched(rid)
        elif state == CostState.CONFIRMED_CHARGED.value:
            await repo.mark_dispatched(rid)
            await repo.mark_spent(rid, raw_output_key=f"raw/{rid}")
        elif state == CostState.ASSUMED_CHARGED.value:
            await repo.mark_dispatched(rid)
            await repo.mark_spent(rid, raw_output_key=None, reconciled_by="conservative")
        elif state == CostState.NOT_CHARGED.value:
            await repo.mark_dispatched(rid)
            await repo.mark_spent(rid, raw_output_key=None, reconciled_by=NOT_ACCEPTED_RECONCILER)
        elif state == "abandoned":
            await repo.mark_dispatched(rid)
            await repo.abandon(rid, reconciled_by="operator:test")
        else:  # pragma: no cover - テストの typo を即座に落とす
            raise AssertionError(f"unknown seed state {state}")
        await session.commit()
    await set_reserved_at(session_factory, rid, reserved_at)
    return rid


async def set_reserved_at(
    session_factory: async_sessionmaker[AsyncSession], reservation_id: str, when: datetime
) -> None:
    async with session_factory() as session:
        await session.execute(
            update(ProviderReservationRow)
            .where(ProviderReservationRow.id == uuid.UUID(reservation_id))
            .values(reserved_at=when)
        )
        await session.commit()


async def reserve_capped(
    session_factory: async_sessionmaker[AsyncSession],
    estimate: Decimal | str | None,
    *,
    cap: Decimal | None = CAP,
    now: datetime | None = NOW,
    provider: ProviderCall = ProviderCall.FAL_IMAGE,
    episode_id: str | None = None,
    key: str | None = None,
    input_hash: str | None = None,
) -> Any:
    """本番と同じ形（reserve → commit）で cap つき予約を 1 回行う。拒否は例外のまま上げる。"""
    episode_id = episode_id or await new_episode(session_factory)
    est = None if estimate is None else Decimal(str(estimate))
    async with session_factory() as session:
        try:
            row = await ProviderReservationRepository(session).reserve(
                episode_id=episode_id,
                provider=provider,
                idempotency_key=key or f"k-{uuid.uuid4().hex}",
                input_hash=input_hash or uuid.uuid4().hex * 2,
                round=1,
                scene_id="sb1",
                estimated_cost_usd=est,
                monthly_cap_usd=cap,
                now=now,
            )
            await session.commit()
            return row
        except BaseException:
            await session.rollback()
            raise


async def count_rows(
    session_factory: async_sessionmaker[AsyncSession], provider: ProviderCall | None = None
) -> int:
    async with session_factory() as session:
        stmt = select(func.count(ProviderReservationRow.id))
        if provider is not None:
            stmt = stmt.where(ProviderReservationRow.provider == provider.value)
        return (await session.execute(stmt)).scalar_one()


def denial(exc: BaseException) -> dict[str, Any]:
    """拒否例外が運ぶ CapDecision の数値。

    ``ProviderMonthlyBudgetExceededError.reason`` は HoldReason の値（保留の理由列）で、
    CapDecision.reason は ``decision_reason``。両方の命名を読む。
    """
    source = getattr(exc, "decision", exc)
    out = {
        name: getattr(source, name) for name in ("month_counted", "estimate", "cap", "remaining")
    }
    out["reason"] = getattr(exc, "decision_reason", None) or getattr(source, "reason", "")
    return out
