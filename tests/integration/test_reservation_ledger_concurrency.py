"""台帳の並行制御を実 PostgreSQL で踏む（独立レビュー P2-b / ADR-0053）。

- 同じ入力に対する異なる idempotency key の同時 reserve は、advisory lock で直列化される
  （先行者が未 dispatch なら後続を止めない。止めるのは dispatched / confirmed_success の行だけ）
- 同じ予約への異なる outcome の同時 resolve_manual は、片方だけが監査行を残す
TEST_DATABASE_URL (*_test) が無ければ skip する。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.states import ProviderCall
from domain.errors import ResubmitRejectedError
from domain.provider.reconciliation import ManualResolutionError, validate_manual_resolution
from infrastructure.db.models import Base, ProviderReservationReconciliationRow
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReconciliationRepository,
    ProviderReservationRepository,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"ledger_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


async def _reserve(factory, episode_id: str, key: str, gate: asyncio.Event):
    async with factory() as session:
        await gate.wait()
        try:
            await ProviderReservationRepository(session).reserve(
                episode_id=episode_id,
                provider=ProviderCall.FAL_VIDEO,
                idempotency_key=key,
                input_hash="h1",
                round=1,
                scene_id="sb1",
            )
            await session.commit()
            return "reserved"
        except ResubmitRejectedError:
            await session.rollback()
            return "rejected"


async def test_racing_reserves_with_different_keys_are_serialized_without_error(
    pg_session_factory,
) -> None:
    async with pg_session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    # 先行者が dispatched になるまで待たない競合: 両者が空の状態から入る。
    # 先に commit した予約は未 dispatch なので後続を止めない（find_unreconciled の担当）。
    # ここで確かめるのは、同時でもデッドロック・重複例外なく直列化されること。
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(_reserve(pg_session_factory, episode.id, f"k-{i}", gate))
        for i in range(2)
    ]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert set(results) <= {"reserved", "rejected"}


async def _resolve(factory, rid: str, outcome: str, gate: asyncio.Event):
    resolution = validate_manual_resolution(
        outcome=outcome, evidence="e", owner_approval="tok", actor=outcome
    )
    async with factory() as session:
        await gate.wait()
        try:
            await ProviderReconciliationRepository(session).resolve_manual(rid, resolution)
            await session.commit()
            return outcome
        except ManualResolutionError:
            await session.rollback()
            return "conflict"


async def test_racing_manual_resolutions_with_different_outcomes_leave_one_audit_row(
    pg_session_factory,
) -> None:
    async with pg_session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        repo = ProviderReservationRepository(session)
        r = await repo.reserve(
            episode_id=episode.id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="k1",
            input_hash="h1",
            round=1,
            scene_id="sb1",
        )
        await repo.mark_dispatched(r.id)
        await session.commit()
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(_resolve(pg_session_factory, r.id, outcome, gate))
        for outcome in ("success", "failure")
    ]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)
    assert sorted(results).count("conflict") == 1
    async with pg_session_factory() as session:
        audit = (
            await session.execute(
                select(func.count(ProviderReservationReconciliationRow.id)).where(
                    ProviderReservationReconciliationRow.reservation_id == uuid.UUID(r.id)
                )
            )
        ).scalar_one()
    assert audit == 1
