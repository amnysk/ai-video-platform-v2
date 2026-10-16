"""運用異常の永続化（ADR-0027）。同じ日の同じ異常は1行で、通知は1回。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from contracts.schedule_guard import AnomalyKind
from infrastructure.db.models import OperationalAnomalyRow
from infrastructure.db.repositories import OperationalAnomalyRepository

DAY = date(2026, 9, 21)
T0 = datetime(2026, 9, 20, 21, 35, tzinfo=UTC)


async def test_first_record_is_new_and_repeats_only_count(session) -> None:
    repo = OperationalAnomalyRepository(session)
    first = await repo.record(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {"why": "x"}, now=T0)
    await session.commit()
    again = await repo.record(
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {"why": "y"}, now=T0 + timedelta(hours=1)
    )
    await session.commit()

    assert first.is_new is True and first.occurrences == 1
    assert again.is_new is False and again.occurrences == 2
    assert again.id == first.id
    rows = await repo.list_open()
    assert len(rows) == 1
    assert rows[0].detail == {"why": "y"}
    assert rows[0].first_detected_at == T0


async def test_the_same_kind_on_another_day_is_a_separate_row(session) -> None:
    repo = OperationalAnomalyRepository(session)
    await repo.record(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {}, now=T0)
    other = await repo.record(
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY + timedelta(days=1), {}, now=T0
    )
    await session.commit()
    assert other.is_new is True
    assert len(await repo.list_open()) == 2


async def test_notification_is_pending_until_marked_and_survives_repeats(session) -> None:
    repo = OperationalAnomalyRepository(session)
    rec = await repo.record(AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY, DAY, {}, now=T0)
    await session.commit()
    assert [r.id for r in await repo.pending_notifications()] == [rec.id]

    await repo.mark_notified(rec.id, now=T0)
    await repo.record(
        AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY, DAY, {}, now=T0 + timedelta(hours=1)
    )
    await session.commit()
    assert await repo.pending_notifications() == []


async def test_resolve_closes_and_a_recurrence_reopens_as_new(session) -> None:
    repo = OperationalAnomalyRepository(session)
    await repo.record(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {}, now=T0)
    await session.commit()
    assert await repo.resolve(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, now=T0) is True
    assert await repo.resolve(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, now=T0) is False
    await session.commit()
    assert await repo.list_open() == []

    reopened = await repo.record(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {}, now=T0)
    await session.commit()
    assert reopened.is_new is True
    assert len(await repo.list_open()) == 1


async def test_resolve_open_closes_every_open_row_of_the_given_kinds(session) -> None:
    repo = OperationalAnomalyRepository(session)
    await repo.record(AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY, DAY, {}, now=T0)
    await repo.record(AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY, DAY + timedelta(days=1), {}, now=T0)
    await repo.record(AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, DAY, {}, now=T0)
    await session.commit()
    closed = await repo.resolve_open([AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY], now=T0)
    await session.commit()
    assert closed == 2
    assert [r.kind for r in await repo.list_open()] == [
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED.value
    ]


async def test_the_database_rejects_an_unknown_kind_and_a_duplicate_key(session) -> None:
    session.add(
        OperationalAnomalyRow(
            kind="NOT_A_KIND",
            anomaly_date=DAY,
            detail={},
            first_detected_at=T0,
            last_detected_at=T0,
            occurrences=1,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()

    for _ in range(2):
        session.add(
            OperationalAnomalyRow(
                kind=AnomalyKind.SCHEDULE_MISSING.value,
                anomaly_date=DAY,
                detail={},
                first_detected_at=T0,
                last_detected_at=T0,
                occurrences=1,
            )
        )
    with pytest.raises(IntegrityError):
        await session.flush()
