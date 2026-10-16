"""Schedule ガードの純粋ロジック（ADR-0027）。maintenance と emergency の分類が要。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.schedule_guard import (
    DAILY_NEXT_RUN_MAX_GAP_SECONDS,
    MAINTENANCE_NOTE_PREFIX,
    DailyStartStatus,
    MaintenanceMarker,
    ScheduleHealth,
)
from domain.schedule_guard import (
    build_maintenance_note,
    classify_schedule,
    daily_fire_time,
    daily_start_status,
    latest_daily_fire,
    maintenance_deadline,
    parse_maintenance_note,
)

NOW = datetime(2026, 9, 20, 5, 0, tzinfo=UTC)
FUTURE = NOW + timedelta(hours=10)


def _marker(deadline: datetime) -> str:
    return build_maintenance_note(
        MaintenanceMarker(reason="deploy", deadline=deadline.strftime("%Y-%m-%dT%H:%M:%SZ"))
    )


def test_note_round_trips_and_carries_the_prefix() -> None:
    note = _marker(NOW + timedelta(minutes=30))
    assert note.startswith(MAINTENANCE_NOTE_PREFIX)
    marker = parse_maintenance_note(note)
    assert marker is not None
    assert marker.reason == "deploy"
    assert marker.deadline == "2026-09-20T05:30:00Z"


@pytest.mark.parametrize(
    "note",
    [
        "",
        None,
        "paused by operator",
        "avp: daily episode pipeline (ADR-0023)",
        MAINTENANCE_NOTE_PREFIX + "not json",
        MAINTENANCE_NOTE_PREFIX + '{"reason": "x"}',  # deadline 無し
        MAINTENANCE_NOTE_PREFIX + '{"reason": "x", "deadline": "yesterday"}',
        MAINTENANCE_NOTE_PREFIX + '{"reason": "x", "deadline": "2026-09-20T05:30:00"}',  # tz 無し
    ],
)
def test_anything_that_is_not_a_valid_marker_is_not_a_maintenance_pause(note: str | None) -> None:
    """壊れた印は emergency 側へ倒す（自動解除しない方向が安全）。"""
    assert parse_maintenance_note(note) is None


def test_maintenance_deadline_is_now_plus_ttl_in_utc_z() -> None:
    assert maintenance_deadline(NOW, 1800) == "2026-09-20T05:30:00Z"


def test_maintenance_deadline_rejects_ttl_beyond_the_cap_and_non_positive() -> None:
    with pytest.raises(ValueError):
        maintenance_deadline(NOW, 7 * 3600)
    with pytest.raises(ValueError):
        maintenance_deadline(NOW, 0)


def test_running_schedule_with_a_future_next_run_is_healthy() -> None:
    assert (
        classify_schedule(exists=True, paused=False, note="x", next_run=FUTURE, now=NOW)
        is ScheduleHealth.HEALTHY
    )


def test_missing_schedule() -> None:
    assert (
        classify_schedule(exists=False, paused=False, note=None, next_run=None, now=NOW)
        is ScheduleHealth.MISSING
    )


def test_unmarked_pause_is_paused_unexpectedly_even_with_a_stale_maintenance_looking_note() -> None:
    assert (
        classify_schedule(
            exists=True, paused=True, note="paused by operator", next_run=None, now=NOW
        )
        is ScheduleHealth.PAUSED_UNEXPECTEDLY
    )


def test_marked_pause_within_the_deadline_is_in_progress() -> None:
    note = _marker(NOW + timedelta(minutes=5))
    assert (
        classify_schedule(exists=True, paused=True, note=note, next_run=None, now=NOW)
        is ScheduleHealth.MAINTENANCE_IN_PROGRESS
    )


def test_marked_pause_past_the_deadline_is_expired() -> None:
    note = _marker(NOW - timedelta(seconds=1))
    assert (
        classify_schedule(exists=True, paused=True, note=note, next_run=None, now=NOW)
        is ScheduleHealth.MAINTENANCE_EXPIRED
    )


def test_a_marker_in_the_note_of_a_running_schedule_is_ignored() -> None:
    note = _marker(NOW - timedelta(hours=1))
    assert (
        classify_schedule(exists=True, paused=False, note=note, next_run=FUTURE, now=NOW)
        is ScheduleHealth.HEALTHY
    )


@pytest.mark.parametrize(
    "next_run",
    [None, NOW - timedelta(hours=8), NOW + timedelta(seconds=DAILY_NEXT_RUN_MAX_GAP_SECONDS + 1)],
)
def test_running_schedule_with_a_missing_stale_or_far_next_run_is_invalid(
    next_run: datetime | None,
) -> None:
    assert (
        classify_schedule(exists=True, paused=False, note=None, next_run=next_run, now=NOW)
        is ScheduleHealth.NEXT_RUN_INVALID
    )


def test_daily_fire_time_is_local_and_returned_in_utc() -> None:
    fire = daily_fire_time("0 6 * * *", "Asia/Tokyo", datetime(2026, 9, 21).date())
    assert fire == datetime(2026, 9, 20, 21, 0, tzinfo=UTC)


@pytest.mark.parametrize("cron", ["*/5 * * * *", "0 6 * * 1", "0 6,18 * * *", "bad", "0 6 * *"])
def test_only_a_plain_daily_cron_is_supported(cron: str) -> None:
    with pytest.raises(ValueError):
        daily_fire_time(cron, "Asia/Tokyo", NOW.date())


def _status(now: datetime, *, slot: bool = False, workflow: bool = False):  # type: ignore[no-untyped-def]
    slot_date, fire = latest_daily_fire(now, "0 6 * * *", "Asia/Tokyo")
    status = daily_start_status(
        now=now, fire_time=fire, grace_seconds=1800, has_slot=slot, has_workflow=workflow
    )
    return status, slot_date


def test_before_fire_time_plus_grace_nothing_is_due() -> None:
    # 2026-09-21 06:29 JST
    status, slot_date = _status(datetime(2026, 9, 20, 21, 29, tzinfo=UTC))
    assert status is DailyStartStatus.NOT_DUE
    assert slot_date.isoformat() == "2026-09-21"


def test_after_grace_without_slot_or_workflow_is_not_started() -> None:
    status, slot_date = _status(datetime(2026, 9, 20, 21, 31, tzinfo=UTC))
    assert status is DailyStartStatus.NOT_STARTED
    assert slot_date.isoformat() == "2026-09-21"


def test_after_grace_with_a_slot_is_started() -> None:
    status, _ = _status(datetime(2026, 9, 20, 21, 31, tzinfo=UTC), slot=True)
    assert status is DailyStartStatus.STARTED_SLOT


def test_after_grace_with_only_a_workflow_is_started() -> None:
    status, _ = _status(datetime(2026, 9, 20, 21, 31, tzinfo=UTC), workflow=True)
    assert status is DailyStartStatus.STARTED_WORKFLOW


def test_before_todays_fire_time_the_expected_slot_is_yesterdays() -> None:
    # 2026-09-21 05:00 JST: 直近の予定は 9/20 06:00 JST
    slot_date, _ = latest_daily_fire(
        datetime(2026, 9, 20, 20, 0, tzinfo=UTC), "0 6 * * *", "Asia/Tokyo"
    )
    assert slot_date.isoformat() == "2026-09-20"


def test_slot_date_is_the_local_date_not_the_utc_date() -> None:
    # 2026-09-20 23:00 JST = 14:00Z: 予定 06:00 JST は 9/20 のもの
    slot_date, _ = latest_daily_fire(
        datetime(2026, 9, 20, 14, 0, tzinfo=UTC), "0 6 * * *", "Asia/Tokyo"
    )
    assert slot_date.isoformat() == "2026-09-20"
