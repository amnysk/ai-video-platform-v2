"""保留率の警報の記録（ADR-0087 §11）。gate を失敗させない・冪等・判定に触れない。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from contracts.quality import QualityCheck, QualityGate, QualityReason, QualityVerdict
from contracts.schedule_guard import AnomalyKind
from domain.quality.deterministic import QualityInputs, build_deterministic_report
from infrastructure.db.models import Base, EpisodeRow, OperationalAnomalyRow
from infrastructure.db.quality_repositories import QualityRecordRepository
from infrastructure.quality.hold_alarm import record_hold_rate_alarm

T0 = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def factory(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}", poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _seed_report(factory, index: int, verdict: QualityVerdict, reason: QualityReason) -> None:
    episode_id = str(uuid.uuid4())
    async with factory() as s:
        s.add(EpisodeRow(id=uuid.UUID(episode_id), topic="t", status="render_ready"))
        await s.commit()
    gate = QualityGate.AUDIO
    check = QualityCheck(gate, verdict, reason, "fixed detail", True)
    inputs = QualityInputs(episode_id, f"{index:064x}", "b" * 64)
    report = build_deterministic_report(
        inputs,
        now=T0 + timedelta(minutes=index),
        rule_version="r1",
        providers=(lambda _i, c=check: [c],),
    )
    async with factory() as s:
        await QualityRecordRepository(s).save_report(report)
        await s.commit()


async def _anomalies(factory):
    async with factory() as s:
        return (await s.execute(select(OperationalAnomalyRow))).scalars().all()


async def test_five_holds_in_ten_record_one_anomaly_row_with_a_breakdown(factory) -> None:
    for i in range(5):
        await _seed_report(factory, i, QualityVerdict.PASS, QualityReason.OK)
    for i, reason in enumerate(
        [
            QualityReason.INSPECTION_UNAVAILABLE,
            QualityReason.INSPECTION_UNAVAILABLE,
            QualityReason.LICENSE_UNVERIFIED,
            QualityReason.LICENSE_UNVERIFIED,
            QualityReason.LICENSE_UNVERIFIED,
        ],
        start=5,
    ):
        await _seed_report(factory, i, QualityVerdict.REVIEW, reason)
    alarm = await record_hold_rate_alarm(factory, now=T0 + timedelta(hours=1))
    assert alarm is not None
    (row,) = await _anomalies(factory)
    assert row.kind == AnomalyKind.QUALITY_GATE_HOLD_RATE_HIGH.value
    assert row.episode_id is None
    assert row.detail == {
        "window": 10,
        "threshold": 5,
        "holds": 5,
        "quality_holds": 3,
        "inspection_holds": 2,
    }


async def test_repeating_is_idempotent_one_row_per_day(factory) -> None:
    for i in range(6):
        await _seed_report(factory, i, QualityVerdict.REVIEW, QualityReason.LICENSE_UNVERIFIED)
    for _ in range(3):
        await record_hold_rate_alarm(factory, now=T0 + timedelta(hours=1))
    (row,) = await _anomalies(factory)
    assert row.occurrences == 3
    await record_hold_rate_alarm(factory, now=T0 + timedelta(days=1))
    assert len(await _anomalies(factory)) == 2  # 翌日は別の行


async def test_four_holds_do_not_create_an_anomaly(factory) -> None:
    for i in range(6):
        await _seed_report(factory, i, QualityVerdict.PASS, QualityReason.OK)
    for i in range(6, 10):
        await _seed_report(factory, i, QualityVerdict.BLOCK, QualityReason.AUDIO_DEFECT)
    assert await record_hold_rate_alarm(factory, now=T0 + timedelta(hours=1)) is None
    assert await _anomalies(factory) == []


async def test_the_alarm_is_resolved_when_the_rate_recovers(factory) -> None:
    for i in range(5):
        await _seed_report(factory, i, QualityVerdict.REVIEW, QualityReason.LICENSE_UNVERIFIED)
    now = T0 + timedelta(hours=1)
    await record_hold_rate_alarm(factory, now=now)
    for i in range(5, 15):
        await _seed_report(factory, i, QualityVerdict.PASS, QualityReason.OK)
    assert await record_hold_rate_alarm(factory, now=now + timedelta(hours=1)) is None
    (row,) = await _anomalies(factory)
    assert row.resolved_at is not None


async def test_a_failure_to_record_never_raises(factory, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken():  # type: ignore[no-untyped-def]
        raise RuntimeError("db gone")

    assert await record_hold_rate_alarm(broken, now=T0) is None  # type: ignore[arg-type]
