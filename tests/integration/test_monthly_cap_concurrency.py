"""月間 hard cap の並行性を実 PostgreSQL で踏む（INV-86 / ADR-0063）。

月ロック（pg_advisory_xact_lock）が無ければ、「合計を読む → INSERT」が同時に走り、
どの 1 本も cap を超えていないのに合算で超える（check-then-act）。ここはそれを踏む。
TEST_DATABASE_URL (*_test) が無ければ skip する（SQLite の論理テストは
tests/unit/test_monthly_cap_ledger.py）。理由は docs/testing/fal-monthly-cap-rationale.md。
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.states import ProviderCall
from domain.errors import ProviderMonthlyBudgetExceededError, ResubmitRejectedError
from infrastructure.db.models import Base, ProviderReservationRow
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from tests.support.db import assert_destructive_allowed, require_test_database_url

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)

D = Decimal
CAP = D("60")


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"cap_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "",
        connect_args={"options": f"-c search_path={schema}"},
        pool_size=20,
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


async def _reserve(
    factory,
    gate: asyncio.Event,
    estimate: str,
    *,
    provider: ProviderCall = ProviderCall.FAL_IMAGE,
    episode_id: str | None = None,
    key: str | None = None,
    input_hash: str | None = None,
) -> str:
    episode_id = episode_id or await _episode(factory)
    async with factory() as session:
        await gate.wait()
        try:
            await ProviderReservationRepository(session).reserve(
                episode_id=episode_id,
                provider=provider,
                idempotency_key=key or f"k-{uuid.uuid4().hex}",
                input_hash=input_hash or uuid.uuid4().hex * 2,
                round=1,
                scene_id="sb1",
                estimated_cost_usd=D(estimate),
                monthly_cap_usd=CAP,
            )
            await session.commit()
            return "reserved"
        except ProviderMonthlyBudgetExceededError:
            await session.rollback()
            return "capped"
        except (IntegrityError, ResubmitRejectedError):
            await session.rollback()
            return "duplicate"


async def _race(coros_factory, count: int) -> list[str]:
    gate = asyncio.Event()
    tasks = [asyncio.create_task(coros_factory(i, gate)) for i in range(count)]
    await asyncio.sleep(0.2)  # 全員が session を開いて gate で待つ
    gate.set()
    return list(await asyncio.wait_for(asyncio.gather(*tasks), timeout=60))


async def _counted(factory) -> Decimal:
    async with factory() as session:
        column = ProviderReservationRow.estimated_cost_usd
        total = (await session.execute(select(func.coalesce(func.sum(column), 0)))).scalar_one()
    return D(str(total))


@pytest.mark.parametrize("racers", [2, 5, 12])
async def test_concurrent_reservations_never_exceed_the_cap(pg_session_factory, racers) -> None:
    """見積 20 を同時に racers 本。cap 60 なので成功は高々 3 本、合計は 60 を超えない。"""
    results = await _race(lambda i, g: _reserve(pg_session_factory, g, "20"), racers)
    assert results.count("reserved") == min(racers, 3)
    assert results.count("capped") == max(racers - 3, 0)
    assert await _counted(pg_session_factory) <= CAP


async def test_two_racers_for_the_last_slot_admit_exactly_one(pg_session_factory) -> None:
    gate_seed = asyncio.Event()
    gate_seed.set()
    assert await _reserve(pg_session_factory, gate_seed, "30") == "reserved"
    results = await _race(lambda i, g: _reserve(pg_session_factory, g, "30"), 2)
    assert sorted(results) == ["capped", "reserved"]  # 30 + 30 + 30 は cap 60 を超える
    assert await _counted(pg_session_factory) == CAP  # ちょうど cap


async def test_image_and_video_racers_share_the_one_family_cap(pg_session_factory) -> None:
    providers = [ProviderCall.FAL_IMAGE, ProviderCall.FAL_VIDEO] * 3
    results = await _race(
        lambda i, g: _reserve(pg_session_factory, g, "25", provider=providers[i]), 6
    )
    assert results.count("reserved") == 2  # 25 * 2 = 50、3 本目は 75 > 60
    assert await _counted(pg_session_factory) <= CAP


async def test_racing_replays_of_one_key_are_neither_double_counted_nor_cap_denied(
    pg_session_factory,
) -> None:
    """同じ idempotency key の同時実行（再実行 / 再起動の重なり）。見積 60 = cap ちょうど。"""
    episode_id = await _episode(pg_session_factory)
    results = await _race(
        lambda i, g: _reserve(
            pg_session_factory,
            g,
            "60",
            episode_id=episode_id,
            key="same-key",
            input_hash="a" * 64,
        ),
        4,
    )
    assert "capped" not in results  # 既に入っている行を cap 超過として拒否しない
    assert results.count("reserved") == 1
    assert await _counted(pg_session_factory) == CAP  # 二重計上なし


async def test_mixed_input_and_month_locks_do_not_deadlock(pg_session_factory) -> None:
    """同じ入力に別 key を重ねる（入力ロックが競合する）状況でも、固定順序（月 → 入力）で
    デッドロックしない。タイムアウト（60 秒）なしで全員が終わること。"""
    episode_id = await _episode(pg_session_factory)
    results = await _race(
        lambda i, g: _reserve(
            pg_session_factory,
            g,
            "5",
            episode_id=episode_id,
            key=f"k-{i}",
            input_hash="b" * 64,
        ),
        8,
    )
    assert set(results) <= {"reserved", "duplicate", "capped"}
    assert await _counted(pg_session_factory) <= CAP
