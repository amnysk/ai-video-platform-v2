from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from infrastructure.db.models import Base
from tests.support.db import install_destructive_statement_guard, mark_test_database

# 破壊的な文（DROP TABLE|SCHEMA|DATABASE / TRUNCATE）は、PostgreSQL では実行直前に
# **実際の接続先** を検証する（INV-83）。drop_all も生 SQL も connect_args を上書きした
# engine もここを通る。SQLite は対象外
install_destructive_statement_guard()

# 印の付与は CI など明示した環境だけ（`AVP_MARK_TEST_DB=1`）。本番インスタンス上の
# avp_test への付与は所有者の手作業（docs/operations/db-role-separation.md）
if os.environ.get("AVP_MARK_TEST_DB") == "1" and os.environ.get("TEST_DATABASE_URL"):
    mark_test_database(os.environ["TEST_DATABASE_URL"])


@pytest_asyncio.fixture
async def engine() -> AsyncIterator:
    """SQLiteのインメモリDB。実PostgreSQL版は tests/integration/。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


@pytest.fixture
def artifact_store():
    from infrastructure.storage.memory_store import InMemoryArtifactStore

    return InMemoryArtifactStore()
