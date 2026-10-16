"""TopicPlan の永続化を実PostgreSQLで踏む（ADR-0025 / INV-22 / INV-24）。

- 同じ (plan_date, strategy, content) の並行保存は1行だけ。両方の呼び出し側が同じ id を得る
- 同じ plan を持つ並行 claim は Episode を1本だけ作る（``uq_episodes_topic_plan_id``）
- Content Memory は全 plan と cancelled 以外の Episode を対象にする
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.operations import ClaimOutcome
from contracts.states import EpisodeStatus
from contracts.topic_planning import TopicPlanStatus
from infrastructure.db.models import Base, EpisodeRow, TopicCandidateRow, TopicPlanRow
from infrastructure.db.repositories import (
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    TopicPlan,
    TopicPlanRepository,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.unit.test_topic_plan_repositories import candidates, new_plan

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL (*_test) must point at PostgreSQL (docker compose --profile core)",
)

DAY = date(2026, 9, 18)


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"topic_test_{uuid.uuid4().hex[:12]}"
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


class _StaleFirstRead(TopicPlanRepository):
    """最初の find が「まだ無い」を返す（両方が同時に空を読んだ状態を確実に作る）。"""

    def __init__(self, session, gate: asyncio.Event) -> None:
        super().__init__(session)
        self._gate = gate
        self._stale = True

    async def find(self, plan_date, strategy_profile_id, content_profile_id) -> TopicPlan | None:
        if self._stale:
            self._stale = False
            await self._gate.wait()
            return None
        return await super().find(plan_date, strategy_profile_id, content_profile_id)


async def _save(factory, gate: asyncio.Event, topic: str, subject: str):
    async with factory() as session:
        repo = _StaleFirstRead(session, gate)
        saved = await repo.save_plan(
            new_plan(topic=topic, subject=subject), candidates(subject), selected_ordinal=1
        )
        await session.commit()
        return saved


async def test_racing_saves_make_one_plan_and_share_its_id(pg_session_factory) -> None:
    """INV-22: 並行保存でも plan は1行。負けた側は勝者の plan を reused=True で受け取る。"""
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(_save(pg_session_factory, gate, "Why samurai wore two swords", "a")),
        asyncio.create_task(_save(pg_session_factory, gate, "A different topic", "b")),
    ]
    await asyncio.sleep(0.05)
    gate.set()
    results = await asyncio.gather(*tasks)

    assert len({r.plan.id for r in results}) == 1
    assert sorted(r.reused for r in results) == [False, True]
    winner = next(r for r in results if not r.reused)
    assert all(r.plan.topic == winner.plan.topic for r in results)
    async with pg_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(TopicPlanRow)) == 1
        # 負けた側の候補は savepoint と一緒に消えている
        assert await session.scalar(select(func.count()).select_from(TopicCandidateRow)) == 2


async def test_rerun_after_commit_reuses_the_plan(pg_session_factory) -> None:
    async with pg_session_factory() as session:
        repo = TopicPlanRepository(session)
        first = await repo.save_plan(new_plan(), candidates(), selected_ordinal=1)
        await session.commit()
    async with pg_session_factory() as session:
        repo = TopicPlanRepository(session)
        again = await repo.save_plan(
            new_plan(topic="Other topic", subject="other"), candidates("other"), selected_ordinal=1
        )
        found = await repo.find(DAY, "us_young_history_v1", "shorts")
    assert again.reused is True and again.plan.id == first.plan.id
    assert found is not None and found.topic == first.plan.topic


async def _claim(factory, trigger: str, plan_id: str, gate: asyncio.Event):
    async with factory() as session:
        repo = DailyEpisodeSlotRepository(session)
        await repo.list_for_date(DAY)
        await gate.wait()
        claim = await repo.claim(
            slot_date=DAY, trigger_id=trigger, daily_limit=2, topic="t", topic_plan_id=plan_id
        )
        await session.commit()
        return claim


async def test_racing_claims_with_one_plan_make_one_episode(pg_session_factory) -> None:
    async with pg_session_factory() as session:
        plan = (
            await TopicPlanRepository(session).save_plan(
                new_plan(), candidates(), selected_ordinal=1
            )
        ).plan
        await session.commit()
    gate = asyncio.Event()
    tasks = [
        asyncio.create_task(_claim(pg_session_factory, f"t-{i}", plan.id, gate)) for i in range(2)
    ]
    await asyncio.sleep(0.05)
    gate.set()
    claims = await asyncio.gather(*tasks)

    assert sum(c.outcome is ClaimOutcome.CREATED for c in claims) == 1
    async with pg_session_factory() as session:
        episodes = list(await session.scalars(select(EpisodeRow)))
        stored = await TopicPlanRepository(session).get(plan.id)
    assert len(episodes) == 1
    assert episodes[0].topic_plan_id == uuid.UUID(plan.id)
    assert {c.episode_id for c in claims if c.episode_id} == {str(episodes[0].id)}
    assert stored is not None and stored.status is TopicPlanStatus.ASSIGNED


async def test_content_memory_scope_on_postgres(pg_session_factory) -> None:
    """INV-24: planned / 制作中の Episode も数え、cancelled は数えない。plan 付きは plan で1回。"""
    async with pg_session_factory() as session:
        plan = (
            await TopicPlanRepository(session).save_plan(
                new_plan(), candidates(), selected_ordinal=1
            )
        ).plan
        episodes = EpisodeRepository(session)
        linked = await episodes.create(topic=plan.topic, topic_plan_id=plan.id)
        await episodes.create(topic="Planned but not uploaded yet")
        cancelled = await episodes.create(topic="Cancelled one")
        for ep, status in (
            (linked.id, EpisodeStatus.IN_PROGRESS),
            (cancelled.id, EpisodeStatus.CANCELLED),
        ):
            await session.execute(
                update(EpisodeRow).where(EpisodeRow.id == uuid.UUID(ep)).values(status=status.value)
            )
        await session.commit()
        memory = await TopicPlanRepository(session).list_memory()
    by_topic = {m.topic: m for m in memory}
    assert set(by_topic) == {plan.topic, "Planned but not uploaded yet"}
    assert by_topic[plan.topic].status == "in_progress"
    assert by_topic[plan.topic].subject == "daisho"
    assert by_topic["Planned but not uploaded yet"].status == "planned"
