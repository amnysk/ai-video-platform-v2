"""precheck の純粋な判定（verdict・見積り・Schedule 窓）。

理由: docs/testing/backup-restore-rationale.md。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain.backup.precheck import (
    Check,
    Status,
    ddl_like_sessions,
    estimate,
    next_safe_start,
    overall,
    pg_major_from_dump_version,
    pg_major_from_num,
    required_bytes,
    schedule_conflicts,
)

DAILY = "0 6 * * *"
WATCHDOG = "35 * * * *"
TZ = "Asia/Tokyo"


def c(status: Status) -> Check:
    return Check("x", status, "d")


def test_verdict_is_the_worst_status_and_empty_is_unknown() -> None:
    assert overall([c(Status.PASS), c(Status.PASS)]) is Status.PASS
    assert overall([c(Status.PASS), c(Status.UNKNOWN)]) is Status.UNKNOWN
    assert overall([c(Status.UNKNOWN), c(Status.FAIL), c(Status.PASS)]) is Status.FAIL
    assert overall([]) is Status.UNKNOWN  # 何も確認していないものを PASS にしない


def test_required_space_is_sources_times_two() -> None:
    assert required_bytes(100) == 200
    assert required_bytes(0) == 0


def test_estimate_grows_with_data_and_is_never_zero() -> None:
    small = estimate(db_bytes=1, object_bytes=1, object_count=1)
    big = estimate(db_bytes=10**10, object_bytes=10**11, object_count=10**5)
    assert 0 < small["seconds"] < big["seconds"]


def test_pg_versions() -> None:
    assert pg_major_from_dump_version("pg_dump (PostgreSQL) 17.11\n") == 17
    assert pg_major_from_dump_version("pg_dump (PostgreSQL) 9.6.24") == 9
    assert pg_major_from_dump_version("garbage") is None
    assert pg_major_from_num("170011") == 17
    assert pg_major_from_num("x") is None


def test_ddl_sessions_are_found_without_returning_query_text() -> None:
    rows = [
        ["101", "active", "400", "ALTER TABLE episodes ADD COLUMN secret_x int"],
        ["102", "active", "5", "select 1"],
        ["103", "idle in transaction", "900", "CREATE INDEX i ON t(a)"],
        ["104", "active", "2", "  vacuum full t"],
    ]
    found = ddl_like_sessions(rows, min_seconds=60)
    assert [f["pid"] for f in found] == ["101", "103"]
    assert all("secret_x" not in str(f) for f in found)
    assert found[0]["statement"] == "ALTER"


def test_the_daily_run_at_21utc_is_a_conflict_and_the_watchdog_35_is_too() -> None:
    # 06:00 JST == 21:00 UTC
    at_21 = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
    assert schedule_conflicts(at_21, 600, DAILY, TZ, WATCHDOG)
    # 20:50 から 10 分で daily の直前に入る
    assert schedule_conflicts(at_21 - timedelta(minutes=10), 600, DAILY, TZ, WATCHDOG)
    watch = datetime(2026, 10, 21, 3, 34, tzinfo=UTC)
    assert any("watchdog" in x for x in schedule_conflicts(watch, 120, DAILY, TZ, WATCHDOG))
    quiet = datetime(2026, 10, 21, 4, 5, tzinfo=UTC)
    assert schedule_conflicts(quiet, 600, DAILY, TZ, WATCHDOG) == []


def test_a_long_backup_spanning_the_daily_window_conflicts() -> None:
    start = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    assert schedule_conflicts(start, 4 * 3600, DAILY, TZ, WATCHDOG)


def test_next_safe_start_does_not_conflict() -> None:
    now = datetime(2026, 10, 21, 21, 5, tzinfo=UTC)
    start = next_safe_start(now, 900, DAILY, TZ, WATCHDOG)
    assert start is not None and start >= now
    assert schedule_conflicts(start, 900, DAILY, TZ, WATCHDOG) == []
