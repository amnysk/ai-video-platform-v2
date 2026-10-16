"""日次 Episode 枠の並行 claim を実PostgreSQLで踏む（ADR-0021）。

SQLite は書き込みを直列化するので競合が起きない。PostgreSQL の read committed で
2つのセッションが同時に「0本」を読んでも、主キーで Episode は1本だけになること。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.operations import ClaimOutcome
from infrastructure.db.models import Base, EpisodeRow
from infrastructure.db.repositories import DailyEpisodeSlotRepository
from tests.support.db import assert_destructive_allowed, require_test_database_url

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)

DAY = date(2026, 9, 15)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"slot_test_{uuid.uuid4().hex[:12]}"
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


async def _claim(factory, trigger: str, gate: asyncio.Event):
    async with factory() as session:
        repo = DailyEpisodeSlotRepository(session)
        await repo.list_for_date(DAY)  # 両方が「まだ0本」を読んだ状態を作る
        await gate.wait()
        claim = await repo.claim(slot_date=DAY, trigger_id=trigger, daily_limit=1, topic=None)
        await session.commit()
        return claim


async def test_racing_claims_with_limit_one_make_exactly_one_episode(pg_session_factory) -> None:
    for _ in range(5):
        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(_claim(pg_session_factory, f"t-{uuid.uuid4().hex}", gate))
            for _ in range(2)
        ]
        await asyncio.sleep(0.05)
        gate.set()
        claims = await asyncio.gather(*tasks)

        created = [c for c in claims if c.outcome is ClaimOutcome.CREATED]
        assert len(created) <= 1
        assert all(
            c.outcome in (ClaimOutcome.CREATED, ClaimOutcome.RESUME, ClaimOutcome.LIMIT_REACHED)
            for c in claims
        )
        async with pg_session_factory() as session:
            slots = await DailyEpisodeSlotRepository(session).list_for_date(DAY)
            episodes = (await session.execute(select(func.count(EpisodeRow.id)))).scalar_one()
        assert len(slots) == 1
        assert episodes == 1
        assert {c.episode_id for c in claims if c.episode_id} <= {slots[0].episode_id}


async def test_racing_retries_of_one_trigger_make_one_slot(pg_session_factory) -> None:
    gate = asyncio.Event()
    tasks = [asyncio.create_task(_claim(pg_session_factory, "same", gate)) for _ in range(3)]
    await asyncio.sleep(0.05)
    gate.set()
    claims = await asyncio.gather(*tasks)
    assert len({c.episode_id for c in claims}) == 1
    assert sum(c.outcome is ClaimOutcome.CREATED for c in claims) == 1
