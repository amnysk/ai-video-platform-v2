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


@pytest.fixture(autouse=True)
def _api_requests_run_as_the_owner_unless_testing_auth(request, monkeypatch):
    """API のテストは認証済み（所有者）で走る。実際の認証を試す試験は ``real_api_auth`` を付ける。

    ADR-0080 §B: 本番の API は変更系に bearer token を要求する。個々の API テストは認証ではなく
    各ルートの振る舞いを試すので、認証の依存だけを所有者として上書きする
    （上書きは ``create_app()`` の戻り値の ``dependency_overrides`` だけ。本番コードに迂回は無い）。
    """
    if request.node.get_closest_marker("real_api_auth"):
        return
    import apps.api.main as api_main
    from apps.api.auth import Actor, require_api_actor

    original = api_main.create_app

    def create_app_as_owner(*args, **kwargs):
        app = original(*args, **kwargs)

        async def as_owner() -> Actor:
            return Actor.OWNER

        app.dependency_overrides[require_api_actor] = as_owner
        return app

    monkeypatch.setattr(api_main, "create_app", create_app_as_owner)
