"""watchdog Schedule の定義と、Schedule 更新が pause を黙って外さないこと（ADR-0027）。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    ScheduleState,
)

from contracts.pipeline import DailyEpisodeInput
from contracts.schedule_guard import (
    DEFAULT_WATCHDOG_CRON,
    WATCHDOG_SCHEDULE_ID,
    WatchdogRequest,
)
from infrastructure.temporal.schedules import (
    build_watchdog_schedule,
    ensure_daily_episode_schedule,
    ensure_watchdog_schedule,
)


def test_watchdog_schedule_runs_hourly_on_the_pipeline_queue_and_skips_overlap() -> None:
    schedule = build_watchdog_schedule(
        cron=DEFAULT_WATCHDOG_CRON, timezone="Asia/Tokyo", request=WatchdogRequest()
    )
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == "DailyWatchdogWorkflow"
    assert action.task_queue == "pipeline"
    assert schedule.spec.cron_expressions == ["35 * * * *"]
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.state.paused is False


def test_watchdog_has_its_own_schedule_id() -> None:
    from contracts.pipeline import DAILY_SCHEDULE_ID

    assert WATCHDOG_SCHEDULE_ID != DAILY_SCHEDULE_ID


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_watchdog_schedule(cron="35 * * * *", timezone="Nowhere/X", request=WatchdogRequest())


class _Handle:
    def __init__(self, state: ScheduleState) -> None:
        self.state = state
        self.updated_schedule: Schedule | None = None

    async def update(self, updater):  # type: ignore[no-untyped-def]
        current = SimpleNamespace(
            description=SimpleNamespace(schedule=SimpleNamespace(state=self.state))
        )
        self.updated_schedule = updater(current).schedule


class _Client:
    def __init__(self, state: ScheduleState) -> None:
        self.handle = _Handle(state)

    async def create_schedule(self, schedule_id, schedule):  # type: ignore[no-untyped-def]
        raise ScheduleAlreadyRunningError()

    def get_schedule_handle(self, schedule_id):  # type: ignore[no-untyped-def]
        return self.handle


def _updated(client: _Client) -> Schedule:
    assert client.handle.updated_schedule is not None
    return client.handle.updated_schedule


async def test_updating_the_daily_schedule_keeps_an_operator_pause() -> None:
    """``ensure-daily-schedule --apply`` が emergency pause を解除してはならない。"""
    paused = ScheduleState(note="operator stop", paused=True)
    client = _Client(paused)
    outcome = await ensure_daily_episode_schedule(
        client,  # type: ignore[arg-type]
        schedule_id="avp-daily-episode",
        cron="0 6 * * *",
        timezone="Asia/Tokyo",
        daily_limit=1,
        workflow_input=DailyEpisodeInput(),
    )
    assert outcome == "updated"
    assert _updated(client).state.paused is True
    assert _updated(client).state.note == "operator stop"


async def test_updating_the_watchdog_schedule_keeps_its_pause_too() -> None:
    client = _Client(ScheduleState(note="operator stop", paused=True))
    await ensure_watchdog_schedule(
        client,  # type: ignore[arg-type]
        cron="35 * * * *",
        timezone="Asia/Tokyo",
        request=WatchdogRequest(),
    )
    assert _updated(client).state.paused is True


async def test_registering_paused_explicitly_still_pauses() -> None:
    client = _Client(ScheduleState(note="running", paused=False))
    await ensure_daily_episode_schedule(
        client,  # type: ignore[arg-type]
        schedule_id="avp-daily-episode",
        cron="0 6 * * *",
        timezone="Asia/Tokyo",
        daily_limit=1,
        workflow_input=DailyEpisodeInput(),
        paused=True,
    )
    assert _updated(client).state.paused is True


def test_watchdog_catchup_is_short() -> None:
    schedule = build_watchdog_schedule(
        cron="35 * * * *", timezone="Asia/Tokyo", request=WatchdogRequest()
    )
    assert schedule.policy.catchup_window <= timedelta(hours=1)
