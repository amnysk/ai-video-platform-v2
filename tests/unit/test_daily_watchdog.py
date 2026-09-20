"""Daily watchdog（ADR-0027）: 「今日の自動運転は始まったか」を Schedule とは別に確かめる。

Test D（pause のままなら検知・slot が無ければ DAILY_AUTOMATION_NOT_STARTED・slot があれば健全）と、
1日1回の通知・再発・通知失敗の再送・emergency pause を解除しないこと。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from contracts.operations import OperationalSwitch
from contracts.schedule_guard import (
    AnomalyKind,
    DailyStartStatus,
    ScheduleHealth,
    WatchdogCheckRequest,
)
from infrastructure.db.repositories import (
    DailyEpisodeSlotRepository,
    OperationalAnomalyRepository,
    OperationalSwitchRepository,
)
from infrastructure.observability.anomaly_notifier import AnomalyNotice
from infrastructure.temporal.schedule_guard import begin_maintenance
from infrastructure.temporal.watchdog import run_daily_watchdog
from tests.support.schedule_control import FakeScheduleControl

SCHEDULE = "avp-daily-episode"
#: 2026-09-21 06:35 JST。予定 06:00 + 猶予 30 分の後
AFTER_GRACE = datetime(2026, 9, 20, 21, 35, tzinfo=UTC)
#: 2026-09-21 06:10 JST。猶予の中
INSIDE_GRACE = datetime(2026, 9, 20, 21, 10, tzinfo=UTC)
SLOT_DATE = date(2026, 9, 21)


class RecordingNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.notices: list[AnomalyNotice] = []
        self.fail = fail

    async def notify(self, notice: AnomalyNotice) -> None:
        if self.fail:
            raise RuntimeError("notifier down")
        self.notices.append(notice)


class FakeCounter:
    def __init__(self, count: int = 0, *, error: bool = False) -> None:
        self.count = count
        self.error = error
        self.queries: list[tuple[str, datetime]] = []

    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        self.queries.append((workflow_type, since))
        if self.error:
            raise RuntimeError("visibility unavailable")
        return self.count


def _request(now: datetime) -> WatchdogCheckRequest:
    return WatchdogCheckRequest(now=now.isoformat())


async def _run(session_factory, control, now, *, counter=None, notifier=None):  # type: ignore[no-untyped-def]
    notifier = notifier or RecordingNotifier()
    result = await run_daily_watchdog(
        control=control,
        session_factory=session_factory,
        workflow_counter=counter or FakeCounter(),
        notifier=notifier,
        request=_request(now),
    )
    return result, notifier


def _control(now: datetime, **kwargs) -> FakeScheduleControl:  # type: ignore[no-untyped-def]
    return FakeScheduleControl(clock=[now], **kwargs)


async def _open(session_factory):  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        return await OperationalAnomalyRepository(session).list_open()


async def _add_slot(session_factory, trigger: str = "daily-episode-x") -> None:  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        await DailyEpisodeSlotRepository(session).claim(
            slot_date=SLOT_DATE, trigger_id=trigger, daily_limit=1, topic="t"
        )
        await session.commit()


async def test_slot_present_after_grace_is_healthy_with_no_anomaly(session_factory) -> None:
    await _add_slot(session_factory)
    result, notifier = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    assert result.daily_start == DailyStartStatus.STARTED_SLOT.value
    assert result.schedule_health == ScheduleHealth.HEALTHY.value
    assert result.anomalies == []
    assert await _open(session_factory) == []
    assert notifier.notices == []


async def test_no_slot_and_no_workflow_records_daily_automation_not_started(
    session_factory,
) -> None:
    result, notifier = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    assert result.daily_start == DailyStartStatus.NOT_STARTED.value
    assert result.slot_date == "2026-09-21"
    assert result.anomalies == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED.value]
    (row,) = await _open(session_factory)
    assert row.kind == "DAILY_AUTOMATION_NOT_STARTED"
    assert row.anomaly_date == SLOT_DATE
    assert [n.kind for n in notifier.notices] == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED]


async def test_the_anomaly_is_recorded_and_notified_once_per_day(session_factory) -> None:
    notifier = RecordingNotifier()
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, notifier=notifier)
    later = AFTER_GRACE + timedelta(hours=1)
    second, _ = await _run(session_factory, _control(later), later, notifier=notifier)
    (row,) = await _open(session_factory)
    assert row.occurrences == 2
    assert len(notifier.notices) == 1
    assert second.anomalies == []  # 新規ではない


async def test_a_workflow_without_a_slot_counts_as_started(session_factory) -> None:
    counter = FakeCounter(count=1)
    result, _ = await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, counter=counter)
    assert result.daily_start == DailyStartStatus.STARTED_WORKFLOW.value
    assert result.anomalies == []
    # 予定時刻（06:00 JST = 前日 21:00Z）以降の起動を数える
    assert counter.queries == [("DailyEpisodeWorkflow", datetime(2026, 9, 20, 21, 0, tzinfo=UTC))]


async def test_inside_the_grace_period_nothing_is_judged(session_factory) -> None:
    result, _ = await _run(session_factory, _control(INSIDE_GRACE), INSIDE_GRACE)
    assert result.daily_start == DailyStartStatus.NOT_DUE.value
    assert await _open(session_factory) == []


async def test_a_paused_schedule_is_detected_and_never_unpaused(session_factory) -> None:
    """Test D: 今回の事故（pause のまま翌朝を迎える）。watchdog は検知するが解除はしない。"""
    control = _control(AFTER_GRACE, paused=True, note="paused for topic-planner deploy")
    result, notifier = await _run(session_factory, control, AFTER_GRACE)
    assert result.schedule_health == ScheduleHealth.PAUSED_UNEXPECTEDLY.value
    assert set(result.anomalies) == {
        AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY.value,
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED.value,
    }
    assert control.paused is True
    assert control.calls == []
    assert result.released_maintenance is False
    assert {n.kind for n in notifier.notices} == {
        AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY,
        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED,
    }


async def test_the_paused_switch_state_is_reported_in_the_anomaly_detail(session_factory) -> None:
    async with session_factory() as session:
        await OperationalSwitchRepository(session).set(OperationalSwitch.PAUSED, True, reason="x")
        await session.commit()
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE)
    (row,) = await _open(session_factory)
    assert row.detail["switch_paused"] is True
    assert row.detail["switch_uploads_paused"] is False
    assert row.detail["schedule_health"] == "healthy"


async def test_an_expired_maintenance_pause_is_released_and_reported(session_factory) -> None:
    start = AFTER_GRACE - timedelta(hours=2)
    control = _control(start)

    async def _sleep(_: float) -> None:
        return None

    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=start, sleep=_sleep
    )
    control.clock[0] = AFTER_GRACE
    await _add_slot(session_factory)
    result, _ = await _run(session_factory, control, AFTER_GRACE)
    assert result.released_maintenance is True
    assert control.paused is False
    assert result.schedule_health == ScheduleHealth.HEALTHY.value
    kinds = {r.kind for r in await _open(session_factory)}
    assert kinds == {AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN.value}


async def test_a_maintenance_pause_inside_its_deadline_is_not_an_anomaly(session_factory) -> None:
    control = _control(INSIDE_GRACE)

    async def _sleep(_: float) -> None:
        return None

    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=INSIDE_GRACE, sleep=_sleep
    )
    result, _ = await _run(session_factory, control, INSIDE_GRACE)
    assert result.schedule_health == ScheduleHealth.MAINTENANCE_IN_PROGRESS.value
    assert result.anomalies == []
    assert control.paused is True


async def test_recovery_resolves_the_open_anomalies(session_factory) -> None:
    paused = _control(AFTER_GRACE, paused=True, note="operator stop")
    await _run(session_factory, paused, AFTER_GRACE)
    assert len(await _open(session_factory)) == 2

    later = AFTER_GRACE + timedelta(hours=1)
    await _add_slot(session_factory)
    healthy = _control(later)
    await _run(session_factory, healthy, later)
    assert await _open(session_factory) == []


async def test_a_missing_schedule_is_recorded(session_factory) -> None:
    result, _ = await _run(session_factory, _control(AFTER_GRACE, exists=False), AFTER_GRACE)
    assert result.schedule_health == ScheduleHealth.MISSING.value
    assert AnomalyKind.SCHEDULE_MISSING.value in result.anomalies


async def test_a_failing_notifier_is_retried_on_the_next_run(session_factory) -> None:
    down = RecordingNotifier(fail=True)
    await _run(session_factory, _control(AFTER_GRACE), AFTER_GRACE, notifier=down)
    async with session_factory() as session:
        assert len(await OperationalAnomalyRepository(session).pending_notifications()) == 1

    up = RecordingNotifier()
    later = AFTER_GRACE + timedelta(hours=1)
    await _run(session_factory, _control(later), later, notifier=up)
    assert [n.kind for n in up.notices] == [AnomalyKind.DAILY_AUTOMATION_NOT_STARTED]
    async with session_factory() as session:
        assert await OperationalAnomalyRepository(session).pending_notifications() == []


async def test_a_visibility_failure_does_not_hide_a_missing_slot(session_factory) -> None:
    result, _ = await _run(
        session_factory,
        _control(AFTER_GRACE),
        AFTER_GRACE,
        counter=FakeCounter(error=True),
    )
    assert result.daily_start == DailyStartStatus.NOT_STARTED.value
    (row,) = await _open(session_factory)
    assert row.detail["workflow_query"] == "failed"


async def test_a_cron_that_is_not_plain_daily_is_reported_but_not_judged(session_factory) -> None:
    request = WatchdogCheckRequest(now=AFTER_GRACE.isoformat(), cron="*/5 * * * *")
    result = await run_daily_watchdog(
        control=_control(AFTER_GRACE),
        session_factory=session_factory,
        workflow_counter=FakeCounter(),
        notifier=RecordingNotifier(),
        request=request,
    )
    assert result.daily_start == DailyStartStatus.UNSUPPORTED_CRON.value
    assert await _open(session_factory) == []
