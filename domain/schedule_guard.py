"""Daily Schedule の pause 分類と日次起動の判定（純粋ロジック。ADR-0027）。

I/O をしない。Temporal・DB へは ``infrastructure/temporal/schedule_guard.py`` が繋ぐ。

安全側の規則: **maintenance pause と証明できないものはすべて emergency pause として扱い、
自動では解除しない**。note が壊れている・期限が読めない・接頭辞が無い、はどれも emergency。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from contracts.schedule_guard import (
    DAILY_NEXT_RUN_MAX_GAP_SECONDS,
    MAINTENANCE_NOTE_PREFIX,
    MAX_MAINTENANCE_TTL_SECONDS,
    DailyStartStatus,
    MaintenanceMarker,
    ScheduleHealth,
)

_DEADLINE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def build_maintenance_note(marker: MaintenanceMarker) -> str:
    payload = {"reason": marker.reason, "deadline": marker.deadline, "owner": marker.owner}
    return MAINTENANCE_NOTE_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _parse_deadline(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        return datetime.strptime(value, _DEADLINE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def parse_maintenance_note(note: str | None) -> MaintenanceMarker | None:
    """有効な maintenance の印だけを返す。それ以外は ``None``（= emergency 扱い）。"""
    if not note or not note.startswith(MAINTENANCE_NOTE_PREFIX):
        return None
    try:
        payload = json.loads(note[len(MAINTENANCE_NOTE_PREFIX) :])
    except ValueError:
        return None
    if not isinstance(payload, dict) or _parse_deadline(payload.get("deadline")) is None:
        return None
    reason = payload.get("reason")
    owner = payload.get("owner", "schedule-guard")
    if not isinstance(reason, str) or not isinstance(owner, str):
        return None
    return MaintenanceMarker(reason=reason, deadline=payload["deadline"], owner=owner)


def maintenance_deadline(now: datetime, ttl_seconds: int) -> str:
    if ttl_seconds <= 0 or ttl_seconds > MAX_MAINTENANCE_TTL_SECONDS:
        raise ValueError(f"ttl must be within 1..{MAX_MAINTENANCE_TTL_SECONDS} seconds")
    return (now.astimezone(UTC) + timedelta(seconds=ttl_seconds)).strftime(_DEADLINE_FORMAT)


def marker_expired(marker: MaintenanceMarker, now: datetime) -> bool:
    deadline = _parse_deadline(marker.deadline)
    return deadline is None or now >= deadline


def classify_schedule(
    *,
    exists: bool,
    paused: bool,
    note: str | None,
    next_run: datetime | None,
    now: datetime,
) -> ScheduleHealth:
    if not exists:
        return ScheduleHealth.MISSING
    if paused:
        marker = parse_maintenance_note(note)
        if marker is None:
            return ScheduleHealth.PAUSED_UNEXPECTEDLY
        if marker_expired(marker, now):
            return ScheduleHealth.MAINTENANCE_EXPIRED
        return ScheduleHealth.MAINTENANCE_IN_PROGRESS
    if next_run is None:
        return ScheduleHealth.NEXT_RUN_INVALID
    gap = (next_run - now).total_seconds()
    if gap <= 0 or gap > DAILY_NEXT_RUN_MAX_GAP_SECONDS:
        return ScheduleHealth.NEXT_RUN_INVALID
    return ScheduleHealth.HEALTHY


def _daily_time(cron: str) -> time:
    """``M H * * *`` だけを受ける（1日1回の Schedule。ADR-0023）。"""
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError(f"unsupported cron for the daily watchdog: {cron!r}")
    try:
        return time(hour=int(fields[1]), minute=int(fields[0]))
    except ValueError as exc:
        raise ValueError(f"unsupported cron for the daily watchdog: {cron!r}") from exc


def daily_fire_time(cron: str, timezone: str, on_date: date) -> datetime:
    """``on_date``（運用タイムゾーンの日付）の予定時刻を UTC で返す。"""
    local = datetime.combine(on_date, _daily_time(cron), tzinfo=ZoneInfo(timezone))
    return local.astimezone(UTC)


def latest_daily_fire(now: datetime, cron: str, timezone: str) -> tuple[date, datetime]:
    """``now`` 以前で最も新しい予定（slot_date, 予定時刻 UTC）。"""
    today = now.astimezone(ZoneInfo(timezone)).date()
    fire = daily_fire_time(cron, timezone, today)
    if fire > now:
        today -= timedelta(days=1)
        fire = daily_fire_time(cron, timezone, today)
    return today, fire


def daily_start_status(
    *,
    now: datetime,
    fire_time: datetime,
    grace_seconds: int,
    has_slot: bool,
    has_workflow: bool,
) -> DailyStartStatus:
    if now < fire_time + timedelta(seconds=grace_seconds):
        return DailyStartStatus.NOT_DUE
    if has_slot:
        return DailyStartStatus.STARTED_SLOT
    if has_workflow:
        return DailyStartStatus.STARTED_WORKFLOW
    return DailyStartStatus.NOT_STARTED
