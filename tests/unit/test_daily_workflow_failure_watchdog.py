"""日次 workflow が失敗し Episode が作られない日の検知（ADR-0054 / INV-68）。

Temporal は fake（``DailyRunReader`` Protocol）。2026-10 の事故（日次 workflow が約9.5秒で
7日連続 FAILED、Episode 無し、anomaly 行ゼロ）の形を、実日付に近い時刻で再現する。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from contracts.schedule_guard import (
    AnomalyKind,
    DailyRunStatus,
    WatchdogCheckRequest,
)
from domain.daily_failure import DailyRunObservation
from infrastructure.db.repositories import (
    DailyEpisodeSlotRepository,
    OperationalAnomalyRepository,
)
from infrastructure.observability.anomaly_notifier import AnomalyNotice
from infrastructure.temporal.watchdog import run_daily_watchdog
from tests.support.schedule_control import FakeScheduleControl

#: 2026-09-21 06:35 JST（予定 06:00 + 猶予の後）
NOW = datetime(2026, 9, 20, 21, 35, tzinfo=UTC)
TODAY = date(2026, 9, 21)
YESTERDAY = date(2026, 9, 20)
TWO_DAYS_AGO = date(2026, 9, 19)
FAILED_KIND = AnomalyKind.DAILY_WORKFLOW_FAILED
UNOBS_KIND = AnomalyKind.DAILY_WORKFLOW_UNOBSERVABLE


def _run(
    day: date,
    status: DailyRunStatus = DailyRunStatus.FAILED,
    *,
    message: str | None = "script step failed",
    minute: int = 0,
) -> DailyRunObservation:
    # 06:00 JST = 前日 21:00 UTC に始まる
    started = datetime(day.year, day.month, day.day, tzinfo=UTC) - timedelta(hours=3)
    return DailyRunObservation(
        slot_date=day,
        workflow_id=f"daily-episode-{day.isoformat()}-{minute}",
        status=status,
        started_at=started + timedelta(minutes=minute),
        failure_type="ApplicationError" if message else None,
        failure_message=message,
    )


class FakeRunReader:
    def __init__(self, runs: list[DailyRunObservation] | None = None, *, error: bool = False):
        self.runs = runs or []
        self.error = error
        self.calls: list[datetime] = []

    async def read_runs(
        self, *, workflow_type: str, schedule_id: str, since: datetime, timezone: str
    ) -> list[DailyRunObservation]:
        self.calls.append(since)
        if self.error:
            raise RuntimeError("temporal visibility and schedule both unreadable")
        return [r for r in self.runs if r.started_at >= since]


class _Notifier:
    def __init__(self) -> None:
        self.notices: list[AnomalyNotice] = []

    async def notify(self, notice: AnomalyNotice) -> None:
        self.notices.append(notice)


class _Counter:
    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        return 1  # 起動はした（DAILY_AUTOMATION_NOT_STARTED を混ぜない）


class _NoOutcomes:
    async def list_stopped_since(self, workflow_type: str, since: datetime) -> list:  # type: ignore[type-arg]
        return []


async def _slot(session_factory, day: date) -> None:  # type: ignore[no-untyped-def]
    """Episode ごと作る代わりに slot を直接 claim する（slot は Episode を伴う）。"""
    async with session_factory() as session:
        await DailyEpisodeSlotRepository(session).claim(
            slot_date=day,
            trigger_id=f"daily-episode-{day.isoformat()}-ok",
            daily_limit=1,
            topic="t",
        )
        await session.commit()


async def _watch(  # type: ignore[no-untyped-def]
    session_factory,
    reader,
    *,
    now: datetime = NOW,
    notifier=None,
    **overrides,
):
    notifier = notifier or _Notifier()
    result = await run_daily_watchdog(
        control=FakeScheduleControl(clock=[now]),
        session_factory=session_factory,
        workflow_counter=_Counter(),
        notifier=notifier,
        request=WatchdogCheckRequest(now=now.isoformat(), **overrides),
        pipeline_outcome_checker=_NoOutcomes(),
        daily_run_reader=reader,
    )
    return result, notifier


async def _rows(session_factory, kind: AnomalyKind, *, only_open: bool = True):  # type: ignore[no-untyped-def]
    async with session_factory() as session:
        repo = OperationalAnomalyRepository(session)
        rows = await repo.list_open([kind])
        return sorted(rows, key=lambda r: r.anomaly_date)


async def test_failed_yesterday_and_today_records_two_consecutive_days(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)  # 日次運用はそれ以前から動いていた
    reader = FakeRunReader([_run(YESTERDAY), _run(TODAY)])
    result, notifier = await _watch(session_factory, reader)

    rows = await _rows(session_factory, FAILED_KIND)
    assert [r.anomaly_date for r in rows] == [YESTERDAY, TODAY]
    assert [r.detail["consecutive_failures"] for r in rows] == [1, 2]
    assert rows[1].detail["status"] == "failed"
    assert rows[1].detail["failure_message"] == "script step failed"
    assert result.anomalies.count(FAILED_KIND.value) == 2
    assert result.consecutive_daily_failures == 2
    assert [n.kind for n in notifier.notices].count(FAILED_KIND) == 2


async def test_completed_running_and_episode_present_record_nothing(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    await _slot(session_factory, TODAY)  # Episode あり: 失敗していても Episode 側の担当
    reader = FakeRunReader(
        [
            _run(TWO_DAYS_AGO, DailyRunStatus.COMPLETED, message=None),
            _run(YESTERDAY, DailyRunStatus.RUNNING, message=None),
            _run(TODAY, DailyRunStatus.FAILED),
        ]
    )
    result, _ = await _watch(session_factory, reader)
    assert await _rows(session_factory, FAILED_KIND) == []
    assert await _rows(session_factory, UNOBS_KIND) == []
    assert result.consecutive_daily_failures == 0


async def test_terminated_and_timed_out_are_recorded_with_their_own_status(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    reader = FakeRunReader(
        [
            _run(YESTERDAY, DailyRunStatus.TERMINATED, message=None),
            _run(TODAY, DailyRunStatus.TIMED_OUT, message=None),
        ]
    )
    await _watch(session_factory, reader)
    rows = await _rows(session_factory, FAILED_KIND)
    assert [r.detail["status"] for r in rows] == ["terminated", "timed_out"]
    assert rows[0].detail["failure_message"] is None  # 取れない原因を捏造しない


async def test_rerun_adds_no_rows_and_is_not_new(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    reader = FakeRunReader([_run(YESTERDAY), _run(TODAY)])
    notifier = _Notifier()
    await _watch(session_factory, reader, notifier=notifier)
    later = NOW + timedelta(hours=1)
    second, _ = await _watch(session_factory, reader, now=later, notifier=notifier)

    rows = await _rows(session_factory, FAILED_KIND)
    assert len(rows) == 2
    assert [r.occurrences for r in rows] == [2, 2]
    assert FAILED_KIND.value not in second.anomalies  # 既に記録済みの日は新規として再発報しない
    assert [n.kind for n in notifier.notices].count(FAILED_KIND) == 2  # 通知も増えない
    assert second.consecutive_daily_failures == 2


async def test_unreadable_temporal_records_unobservable_not_ok(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    result, _ = await _watch(session_factory, FakeRunReader(error=True))
    rows = await _rows(session_factory, UNOBS_KIND)
    assert [r.anomaly_date for r in rows] == [YESTERDAY, TODAY]
    assert rows[0].detail["reason"] == "reader_error"
    assert await _rows(session_factory, FAILED_KIND) == []
    assert UNOBS_KIND.value in result.anomalies


async def test_retention_expired_old_day_is_unobservable_but_latest_day_is_deferred(
    session_factory,
) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    # 昨日の履歴は保持期間で消えた。今日は実行が見つからない（NOT_STARTED の担当）
    await _watch(session_factory, FakeRunReader([]))
    (row,) = await _rows(session_factory, UNOBS_KIND)
    assert row.anomaly_date == YESTERDAY
    assert row.detail["reason"] == "no_execution_in_retention"
    assert await _rows(session_factory, FAILED_KIND) == []


async def test_recorded_failure_survives_retention_expiry(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    await _watch(session_factory, FakeRunReader([_run(YESTERDAY), _run(TODAY)]))
    # 翌時間: Temporal 側の履歴が消えた。確定した FAILED を UNOBSERVABLE に劣化させない
    await _watch(session_factory, FakeRunReader([]), now=NOW + timedelta(hours=1))
    failed = await _rows(session_factory, FAILED_KIND)
    assert [r.anomaly_date for r in failed] == [YESTERDAY, TODAY]
    assert await _rows(session_factory, UNOBS_KIND) == []


async def test_failure_outside_window_is_not_recorded(session_factory) -> None:
    await _slot(session_factory, date(2026, 9, 10))
    reader = FakeRunReader([_run(date(2026, 9, 12)), _run(TWO_DAYS_AGO)])
    await _watch(session_factory, reader, daily_failure_lookback_days=2)  # 窓 = 9/20, 9/21
    assert await _rows(session_factory, FAILED_KIND) == []
    assert min(reader.calls) > _run(TWO_DAYS_AGO).started_at  # 窓より前は読みもしない


async def test_window_boundary_includes_the_oldest_day_and_seeds_the_streak(
    session_factory,
) -> None:
    await _slot(session_factory, date(2026, 9, 10))
    # 9/19 は窓の外（窓=2日）だが、既に 4 連続として未解決で記録されている
    async with session_factory() as session:
        await OperationalAnomalyRepository(session).record(
            FAILED_KIND, TWO_DAYS_AGO, {"consecutive_failures": 4}, now=NOW - timedelta(days=1)
        )
        await session.commit()
    reader = FakeRunReader([_run(YESTERDAY), _run(TODAY)])
    await _watch(session_factory, reader, daily_failure_lookback_days=2)
    rows = await _rows(session_factory, FAILED_KIND)
    assert [(r.anomaly_date, r.detail["consecutive_failures"]) for r in rows[1:]] == [
        (YESTERDAY, 5),
        (TODAY, 6),
    ]


async def test_days_before_the_first_slot_are_not_judged(session_factory) -> None:
    # slot が1つも無い新品の DB: 最新の slot 日だけを見る
    result, _ = await _watch(session_factory, FakeRunReader([_run(YESTERDAY), _run(TODAY)]))
    rows = await _rows(session_factory, FAILED_KIND)
    assert [r.anomaly_date for r in rows] == [TODAY]
    assert rows[0].detail["consecutive_failures"] == 1
    assert result.consecutive_daily_failures == 1


async def test_failure_resolves_when_a_later_run_completes_or_a_slot_appears(
    session_factory,
) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    await _watch(session_factory, FakeRunReader([_run(YESTERDAY), _run(TODAY)]))
    # 昨日は手動再実行が成功、今日は slot（Episode）ができた
    await _slot(session_factory, TODAY)
    recovered = FakeRunReader(
        [
            _run(YESTERDAY),
            _run(YESTERDAY, DailyRunStatus.COMPLETED, message=None, minute=30),
            _run(TODAY),
        ]
    )
    await _watch(session_factory, recovered, now=NOW + timedelta(hours=1))
    assert await _rows(session_factory, FAILED_KIND) == []


async def test_old_open_failure_outside_window_closes_only_when_a_slot_appears(
    session_factory,
) -> None:
    await _slot(session_factory, date(2026, 9, 10))
    old = date(2026, 9, 14)
    async with session_factory() as session:
        await OperationalAnomalyRepository(session).record(
            FAILED_KIND, old, {"consecutive_failures": 1}, now=NOW - timedelta(days=7)
        )
        await session.commit()
    await _watch(session_factory, FakeRunReader([]), daily_failure_lookback_days=1)
    assert [r.anomaly_date for r in await _rows(session_factory, FAILED_KIND)] == [old]
    await _slot(session_factory, old)
    await _watch(session_factory, FakeRunReader([]), daily_failure_lookback_days=1)
    assert await _rows(session_factory, FAILED_KIND) == []


async def test_detail_never_carries_secrets_even_from_a_careless_reader(session_factory) -> None:
    await _slot(session_factory, TWO_DAYS_AGO)
    leaky = _run(
        TODAY,
        message="HTTP 401 api_key=sk-live-ABCDEF1234567890 https://h/x?token=SECRET " + "z" * 500,
    )
    await _watch(session_factory, FakeRunReader([leaky]))
    (row,) = await _rows(session_factory, FAILED_KIND)
    blob = repr(row.detail)
    assert "sk-live" not in blob and "SECRET" not in blob
    assert len(row.detail["failure_message"]) <= 200
