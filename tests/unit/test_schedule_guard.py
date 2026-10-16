"""Schedule ガードの操作（ADR-0027）: maintenance の begin / end / reconcile。

Test B（maintenance pause のあと成功なら running に戻る）、Test C（emergency pause は
自動解除されない）、TTL 切れ、冪等性。Temporal には繋がず fake で確かめる。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.schedule_guard import ScheduleHealth
from domain.schedule_guard import parse_maintenance_note
from infrastructure.temporal.schedule_guard import (
    EXIT_EMERGENCY_PAUSED,
    EXIT_FAILED,
    EXIT_OK,
    begin_maintenance,
    end_maintenance,
    reconcile_schedule,
)
from tests.support.schedule_control import FakeScheduleControl

SCHEDULE = "avp-daily-episode"
T0 = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)


def _control(**kwargs) -> FakeScheduleControl:  # type: ignore[no-untyped-def]
    return FakeScheduleControl(clock=[T0], **kwargs)


async def _no_sleep(_: float) -> None:
    return None


async def test_begin_pauses_with_a_marker_and_deadline() -> None:
    control = _control()
    outcome = await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    assert outcome.exit_code == EXIT_OK
    assert control.paused is True
    marker = parse_maintenance_note(control.note)
    assert marker is not None
    assert marker.reason == "deploy"
    assert marker.deadline == "2026-09-20T05:30:00Z"


async def test_begin_then_end_returns_the_schedule_to_running_and_verifies_next_run() -> None:
    """Test B: deploy 中の一時 pause は、正常終了後に必ず running へ戻る。"""
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    control.clock[0] = T0 + timedelta(minutes=10)
    outcome = await end_maintenance(control, SCHEDULE, now=control.clock[0], sleep=_no_sleep)
    assert outcome.exit_code == EXIT_OK
    assert outcome.health is ScheduleHealth.HEALTHY
    assert control.paused is False
    assert parse_maintenance_note(control.note) is None


async def test_end_fails_loudly_when_the_schedule_is_still_paused_after_unpause() -> None:
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    control.unpause_is_ignored = True
    outcome = await end_maintenance(control, SCHEDULE, now=T0, sleep=_no_sleep)
    assert outcome.exit_code == EXIT_FAILED


async def test_end_fails_when_the_next_run_is_not_in_the_near_future() -> None:
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    control.next_run_after = timedelta(days=3)
    outcome = await end_maintenance(control, SCHEDULE, now=T0, sleep=_no_sleep)
    assert outcome.exit_code == EXIT_FAILED
    assert outcome.health is ScheduleHealth.NEXT_RUN_INVALID


async def test_end_is_idempotent_when_the_schedule_is_already_running() -> None:
    control = _control()
    outcome = await end_maintenance(control, SCHEDULE, now=T0, sleep=_no_sleep)
    assert outcome.exit_code == EXIT_OK
    assert control.calls == []


async def test_begin_refuses_an_emergency_pause_and_never_touches_it() -> None:
    """Test C（begin 側）: 運用者の手動停止を maintenance の印で上書きしない。"""
    control = _control(paused=True, note="stopped by owner: bad provider output")
    outcome = await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    assert outcome.exit_code == EXIT_EMERGENCY_PAUSED
    assert control.calls == []
    assert control.note == "stopped by owner: bad provider output"


async def test_end_never_unpauses_an_emergency_pause() -> None:
    """Test C（end 側）: deploy の後始末が運用者の停止を解除してはならない。"""
    control = _control(paused=True, note="stopped by owner")
    outcome = await end_maintenance(control, SCHEDULE, now=T0, sleep=_no_sleep)
    assert outcome.exit_code == EXIT_EMERGENCY_PAUSED
    assert control.paused is True
    assert control.calls == []


async def test_an_operator_pause_during_maintenance_takes_over_and_survives_end() -> None:
    """maintenance 中に運用者が pause し直すと note が置き換わり、emergency として残る。"""
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    await control.pause(SCHEDULE, "operator: stop everything")  # 運用者の手動 pause
    control.calls.clear()
    outcome = await end_maintenance(control, SCHEDULE, now=T0, sleep=_no_sleep)
    assert outcome.exit_code == EXIT_EMERGENCY_PAUSED
    assert control.paused is True
    assert control.calls == []


async def test_reconcile_never_unpauses_an_emergency_pause_however_old() -> None:
    control = _control(paused=True, note="stopped by owner")
    control.clock[0] = T0 + timedelta(days=30)
    outcome = await reconcile_schedule(control, SCHEDULE, now=control.clock[0])
    assert outcome.health is ScheduleHealth.PAUSED_UNEXPECTEDLY
    assert outcome.released is False
    assert control.paused is True
    assert control.calls == []


async def test_reconcile_leaves_a_maintenance_pause_inside_its_deadline() -> None:
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    control.calls.clear()
    outcome = await reconcile_schedule(control, SCHEDULE, now=T0 + timedelta(minutes=29))
    assert outcome.health is ScheduleHealth.MAINTENANCE_IN_PROGRESS
    assert outcome.released is False
    assert control.calls == []


async def test_reconcile_releases_an_expired_maintenance_pause_and_reports_the_overrun() -> None:
    """TTL 切れ: deploy が end を呼ばずに死んでも、翌日を paused のまま迎えない。"""
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=1800, now=T0, sleep=_no_sleep
    )
    control.clock[0] = T0 + timedelta(minutes=31)
    outcome = await reconcile_schedule(control, SCHEDULE, now=control.clock[0])
    assert outcome.health_before is ScheduleHealth.MAINTENANCE_EXPIRED
    assert outcome.released is True
    assert outcome.health is ScheduleHealth.HEALTHY
    assert control.paused is False


async def test_reconcile_is_idempotent() -> None:
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=60, now=T0, sleep=_no_sleep
    )
    later = T0 + timedelta(minutes=5)
    control.clock[0] = later
    first = await reconcile_schedule(control, SCHEDULE, now=later)
    second = await reconcile_schedule(control, SCHEDULE, now=later)
    assert first.released is True
    assert second.released is False
    assert [c[0] for c in control.calls].count("unpause") == 1


async def test_reconcile_on_a_missing_schedule_reports_missing() -> None:
    control = _control(exists=False)
    outcome = await reconcile_schedule(control, SCHEDULE, now=T0)
    assert outcome.health is ScheduleHealth.MISSING
    assert control.calls == []


async def test_begin_on_a_missing_schedule_fails() -> None:
    control = _control(exists=False)
    outcome = await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=60, now=T0, sleep=_no_sleep
    )
    assert outcome.exit_code == EXIT_FAILED


async def test_begin_again_during_maintenance_extends_the_deadline() -> None:
    control = _control()
    await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=600, now=T0, sleep=_no_sleep
    )
    later = T0 + timedelta(minutes=5)
    outcome = await begin_maintenance(
        control, SCHEDULE, reason="deploy", ttl_seconds=600, now=later, sleep=_no_sleep
    )
    assert outcome.exit_code == EXIT_OK
    marker = parse_maintenance_note(control.note)
    assert marker is not None and marker.deadline == "2026-09-20T05:15:00Z"


async def test_begin_rejects_a_ttl_beyond_the_cap() -> None:
    control = _control()
    with pytest.raises(ValueError):
        await begin_maintenance(
            control, SCHEDULE, reason="deploy", ttl_seconds=10**6, now=T0, sleep=_no_sleep
        )
    assert control.calls == []


async def test_a_forged_prefix_without_a_valid_deadline_is_treated_as_emergency() -> None:
    control = _control(paused=True, note="AVP-MAINTENANCE/1 {broken")
    control.clock[0] = T0 + timedelta(days=1)
    outcome = await reconcile_schedule(control, SCHEDULE, now=control.clock[0])
    assert outcome.released is False
    assert control.paused is True
