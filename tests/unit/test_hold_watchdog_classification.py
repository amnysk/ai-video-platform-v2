"""watchdog が「失敗」「保留」「未納品」を区別する（ADR-0058 / INV-77）。

Temporal は fake、DB は in-memory。保留（残高不足など意図した停止）を失敗として通報せず、
しかし見えなくもせず、長引けば既存の ``EPISODE_STAGE_STALLED`` で通報する（kind を増やさない）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from contracts.daily_gate import OpenHold
from contracts.provider_health import HoldReason
from contracts.schedule_guard import AnomalyKind, DailyRunStatus, WatchdogCheckRequest
from contracts.states import EpisodeStatus
from domain.daily_failure import DailyRunObservation
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import DailyEpisodeSlotRepository, OperationalAnomalyRepository
from infrastructure.temporal.watchdog import run_daily_watchdog
from tests.support.schedule_control import FakeScheduleControl

NOW = datetime(2026, 9, 20, 21, 35, tzinfo=UTC)  # 2026-09-21 06:35 JST
SLOT = date(2026, 9, 21)
CHECK = AnomalyKind.EPISODE_NOT_COMPLETED_BY_DEADLINE
STALL = AnomalyKind.EPISODE_STAGE_STALLED


class _Notifier:
    def __init__(self) -> None:
        self.kinds: list[AnomalyKind] = []

    async def notify(self, notice) -> None:  # type: ignore[no-untyped-def]
        self.kinds.append(notice.kind)


class _Counter:
    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        return 1


class _NoOutcomes:
    async def list_stopped_since(self, workflow_type: str, since: datetime) -> list:  # type: ignore[type-arg]
        return []


class _Runs:
    def __init__(self, runs: list[DailyRunObservation] | None = None) -> None:
        self.runs = runs or []

    async def read_runs(self, **kw) -> list[DailyRunObservation]:  # type: ignore[no-untyped-def]
        return self.runs


class _Holds:
    def __init__(self, holds: list[OpenHold] | None = None, *, error: bool = False) -> None:
        self.holds = holds or []
        self.error = error

    async def list_open_holds(self) -> list[OpenHold]:
        if self.error:
            raise RuntimeError("hold table unreadable")
        return list(self.holds)


def _hold(episode_id: uuid.UUID, *, days: float) -> OpenHold:
    return OpenHold(
        episode_id=str(episode_id),
        held_since=NOW - timedelta(days=days),
        reason=HoldReason.WAITING_FOR_CREDITS.value,
        account_key="fal:default",
        resumable_stage="production",
    )


async def _episode(session_factory, status: EpisodeStatus, *, age_hours: float) -> uuid.UUID:  # type: ignore[no-untyped-def]
    ep = uuid.uuid4()
    at = NOW - timedelta(hours=age_hours)
    async with session_factory() as session:
        session.add(
            EpisodeRow(
                id=ep,
                status=status.value,
                topic="t",
                created_at=at,
                updated_at=at,
                status_changed_at=at,
            )
        )
        await session.commit()
    return ep


async def _slot(session_factory) -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await DailyEpisodeSlotRepository(session).claim(
            slot_date=SLOT, trigger_id="daily-episode-x", daily_limit=1, topic="t"
        )
        await session.commit()


async def _watch(session_factory, holds, *, now=NOW, notifier=None, runs=None):  # type: ignore[no-untyped-def]
    notifier = notifier or _Notifier()
    result = await run_daily_watchdog(
        control=FakeScheduleControl(clock=[now]),
        session_factory=session_factory,
        workflow_counter=_Counter(),
        notifier=notifier,
        request=WatchdogCheckRequest(now=now.isoformat()),
        pipeline_outcome_checker=_NoOutcomes(),
        daily_run_reader=_Runs(runs),
        hold_reader=holds,
    )
    return result, notifier


async def _open(session_factory, *kinds):  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        return await OperationalAnomalyRepository(session).list_open(list(kinds))


async def test_a_recent_hold_is_not_reported_as_an_overdue_episode(session_factory) -> None:
    await _slot(session_factory)
    ep = await _episode(session_factory, EpisodeStatus.STORYBOARD_READY, age_hours=12)
    result, notifier = await _watch(session_factory, _Holds([_hold(ep, days=0.5)]))
    assert await _open(session_factory, CHECK, STALL) == []
    assert notifier.kinds == []
    # ただし見えなくはならない
    assert result.held_episodes == 1
    assert result.held_escalated == 0
    assert result.oldest_hold_days == 0.5


async def test_without_the_hold_the_same_episode_is_an_overdue_failure(session_factory) -> None:
    """対照: 保留の情報が無ければ、従来どおり期限超過として通報される。"""
    await _slot(session_factory)
    await _episode(session_factory, EpisodeStatus.STORYBOARD_READY, age_hours=12)
    result, _ = await _watch(session_factory, _Holds([]))
    assert [r.kind for r in await _open(session_factory, CHECK)] == [CHECK.value]
    assert result.held_episodes == 0


async def test_a_blocked_hold_is_not_a_stall_until_it_is_old(session_factory) -> None:
    await _slot(session_factory)
    ep = await _episode(session_factory, EpisodeStatus.BLOCKED, age_hours=30)
    await _watch(session_factory, _Holds([_hold(ep, days=1.2)]))
    assert await _open(session_factory, STALL) == []


async def test_a_long_hold_escalates_through_the_existing_stalled_kind(session_factory) -> None:
    await _slot(session_factory)
    ep = await _episode(session_factory, EpisodeStatus.STORYBOARD_READY, age_hours=24 * 4)
    holds = _Holds([_hold(ep, days=4)])
    result, notifier = await _watch(session_factory, holds)

    (row,) = await _open(session_factory, STALL)
    assert row.episode_id == ep
    assert row.detail["hold"]["reason"] == "waiting_for_credits"
    assert row.detail["hold"]["resumable_stage"] == "production"
    assert row.detail["current_status"] == "held"
    assert row.detail["resumable"] is True
    assert await _open(session_factory, CHECK) == []  # 二重に通報しない
    assert notifier.kinds == [STALL]
    assert result.held_escalated == 1
    assert STALL.value in result.anomalies

    # 次の毎時: 同じ行（増えない）、再通知しない
    later = NOW + timedelta(hours=1)
    again, notifier2 = await _watch(session_factory, holds, now=later, notifier=notifier)
    (row2,) = await _open(session_factory, STALL)
    assert row2.occurrences == 2
    assert again.anomalies == []
    assert notifier.kinds == [STALL]

    # 保留が解放されたら閉じる（既存の自動 resolve）
    await _watch(session_factory, _Holds([]), now=later + timedelta(hours=1), notifier=notifier)
    assert await _open(session_factory, STALL) == []
    assert notifier2 is notifier


async def test_a_real_failure_is_still_reported_next_to_a_hold(session_factory) -> None:
    await _slot(session_factory)
    held = await _episode(session_factory, EpisodeStatus.STORYBOARD_READY, age_hours=12)
    broken = await _episode(session_factory, EpisodeStatus.BLOCKED, age_hours=5)
    await _watch(session_factory, _Holds([_hold(held, days=0.5)]))
    rows = await _open(session_factory, STALL)
    assert [r.episode_id for r in rows] == [broken]


async def test_an_unreadable_hold_table_does_not_hide_anything(session_factory) -> None:
    await _slot(session_factory)
    await _episode(session_factory, EpisodeStatus.STORYBOARD_READY, age_hours=12)
    result, _ = await _watch(session_factory, _Holds(error=True))
    assert [r.kind for r in await _open(session_factory, CHECK)] == [CHECK.value]
    assert result.held_episodes == 0


async def test_a_day_skipped_for_backlog_is_not_a_daily_failure(session_factory) -> None:
    """日次が HELD_BACKLOG で Episode を作らなかった日（Completed・slot 無し）は失敗日ではない。"""
    started = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
    run = DailyRunObservation(
        slot_date=SLOT,
        workflow_id="daily-episode-2026-09-21",
        status=DailyRunStatus.COMPLETED,
        started_at=started,
        failure_type=None,
        failure_message=None,
    )
    result, notifier = await _watch(session_factory, _Holds([]), runs=[run])
    assert result.anomalies == []
    assert result.consecutive_daily_failures == 0
    assert notifier.kinds == []
