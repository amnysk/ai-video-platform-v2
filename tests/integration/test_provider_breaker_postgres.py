"""breaker と保留の並行制御を実 PostgreSQL で踏む（ADR-0057 / INV-72 / INV-73）。

unit の SQLite は接続を1本共有するので、並行 session の競合は再現できない。ここは別接続の
同時実行で「同時に開いても行は1つ・開いた勝者は1人」「同じ保留は1行」を確かめる。
TEST_DATABASE_URL (*_test) が無ければ skip する。fal には到達しない。
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.provider_health import (
    BreakerState,
    HoldReason,
    ProviderAccount,
    ProviderFamily,
    RefusalKind,
)
from infrastructure.db.models import Base, ProviderBreakerRow, ProviderCreditHoldRow
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from infrastructure.db.repositories import EpisodeRepository
from tests.support.db import assert_destructive_allowed, require_test_database_url

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)

FAL = ProviderAccount(ProviderFamily.FAL)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"breaker_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "",
        connect_args={"options": f"-c search_path={schema}"},
        pool_size=12,
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


async def _episode(factory) -> str:
    async with factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    return str(episode.id)


async def test_simultaneous_opens_leave_one_row_and_exactly_one_winner(pg_session_factory) -> None:
    health = PostgresProviderHealth(pg_session_factory)
    episodes = [await _episode(pg_session_factory) for _ in range(8)]

    async def open_(ep: str) -> bool:
        return await health.open_breaker(
            FAL,
            reason=RefusalKind.BALANCE_EXHAUSTED,
            http_status=403,
            evidence="Exhausted balance",
            episode_id=ep,
        )

    results = await asyncio.gather(*[open_(ep) for ep in episodes])
    assert results.count(True) == 1
    async with pg_session_factory() as session:
        rows = (await session.scalars(select(ProviderBreakerRow))).all()
    assert len(rows) == 1
    assert rows[0].state == BreakerState.OPEN.value and rows[0].refusal_count == 8


async def test_simultaneous_holds_for_the_same_target_make_one_row(pg_session_factory) -> None:
    health = PostgresProviderHealth(pg_session_factory)
    ep = await _episode(pg_session_factory)

    async def hold() -> None:
        await health.hold_episode(
            episode_id=ep,
            scene_id="sb1",
            account=FAL,
            reason=HoldReason.WAITING_FOR_CREDITS,
            resumable_stage="production",
            generation_profile_id=None,
        )

    await asyncio.gather(*[hold() for _ in range(8)])
    async with pg_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(ProviderCreditHoldRow))
    assert count == 1
