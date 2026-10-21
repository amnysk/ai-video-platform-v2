"""日次 workflow の失敗検知の純粋ロジック（ADR-0054 / INV-68）。

I/O をしない。Temporal の読み取りは ``infrastructure/temporal/daily_runs.py``、
記録は ``infrastructure/temporal/watchdog.py``。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from contracts.schedule_guard import (
    DAILY_FAILURE_MESSAGE_LIMIT,
    DAILY_FAILURE_TYPE_LIMIT,
    DAILY_RUN_FAILED_STATUSES,
    DAILY_RUN_OK_STATUSES,
    MAX_DAILY_FAILURE_LOOKBACK_DAYS,
    DailyRunStatus,
)


@dataclass(frozen=True, slots=True)
class DailyRunObservation:
    """日次 workflow の1実行の観測。``failure_*`` は要約済み（secret を含まない）。"""

    slot_date: date
    workflow_id: str
    status: DailyRunStatus
    started_at: datetime
    failure_type: str | None = None
    failure_message: str | None = None


class DayVerdict(StrEnum):
    OK = "ok"
    #: 失敗・タイムアウト・terminate で閉じ、slot が無い
    FAILED = "failed"
    #: 終了状態を確認できない（OK にしない）
    UNOBSERVABLE = "unobservable"
    #: 実行が見つからないが最新 slot 日: ``DAILY_AUTOMATION_NOT_STARTED`` の担当
    DEFERRED = "deferred"


def slot_date_of(started_at: datetime, timezone: str) -> date:
    return started_at.astimezone(ZoneInfo(timezone)).date()


def lookback_dates(latest: date, days: int) -> list[date]:
    """``latest`` で終わる ``days`` 日分（古い順）。1..上限に丸める。"""
    n = max(1, min(days, MAX_DAILY_FAILURE_LOOKBACK_DAYS))
    return [latest - timedelta(days=i) for i in range(n - 1, -1, -1)]


def judge_day(
    *,
    has_slot: bool,
    runs: Sequence[DailyRunObservation],
    readable: bool,
    is_latest_slot_day: bool,
) -> tuple[DayVerdict, DailyRunObservation | None]:
    """その日の判定。FAILED のときだけ、報告する実行（最後に始まった失敗）を返す。"""
    if has_slot:
        return DayVerdict.OK, None  # Episode がある。以降は Episode 単位の検査の担当
    if not readable:
        return DayVerdict.UNOBSERVABLE, None
    if not runs:
        return (DayVerdict.DEFERRED if is_latest_slot_day else DayVerdict.UNOBSERVABLE), None
    if any(r.status in DAILY_RUN_OK_STATUSES for r in runs):
        return DayVerdict.OK, None
    failed = [r for r in runs if r.status in DAILY_RUN_FAILED_STATUSES]
    if failed:
        return DayVerdict.FAILED, max(failed, key=lambda r: r.started_at)
    return DayVerdict.UNOBSERVABLE, None


def failure_streaks(
    days_ascending: Sequence[date], failed_days: set[date], *, prior_streak: int
) -> dict[date, int]:
    """各日で終わる連続失敗日数。健全・未確認の日で 0 に戻る。"""
    streaks: dict[date, int] = {}
    previous = max(prior_streak, 0)
    for day in days_ascending:
        previous = previous + 1 if day in failed_days else 0
        streaks[day] = previous
    return streaks


_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_URL_USERINFO = re.compile(r"(?<=://)[^/\s@]+@")
_URL_QUERY = re.compile(r"(https?://[^\s?#]+)[?#]\S*", re.I)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+\S+")
_KEY_VALUE = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|token|secret|passw(?:or)?d|credential|authorization)[\w-]*)"
    r"\s*[:=]\s*\S+"
)
_KNOWN_PREFIX = re.compile(r"\b(?:sk|pk|rk|fal|ghp|gho|xox[abp])[-_][A-Za-z0-9_\-]{8,}")
_OPAQUE = re.compile(r"[A-Za-z0-9+/_=\-]{32,}")
_SPACE = re.compile(r"\s+")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_failure(
    failure_type: str | None, message: str | None
) -> tuple[str | None, str | None]:
    """失敗の型名と message を、secret を伏せて短くする（INV-20 / INV-39）。"""

    def _clean(raw: str | None, limit: int) -> str | None:
        if not raw or not raw.strip():
            return None
        s = _UUID.sub("<id>", raw)
        s = _URL_USERINFO.sub("<redacted>@", s)
        s = _URL_QUERY.sub(r"\1?<redacted>", s)
        s = _BEARER.sub(lambda m: f"{m.group(1)} <redacted>", s)
        s = _KEY_VALUE.sub(lambda m: f"{m.group(1)}=<redacted>", s)
        s = _KNOWN_PREFIX.sub("<redacted>", s)
        s = _OPAQUE.sub("<redacted>", s)
        return _clip(_SPACE.sub(" ", s).strip(), limit)

    return _clean(failure_type, DAILY_FAILURE_TYPE_LIMIT), _clean(
        message, DAILY_FAILURE_MESSAGE_LIMIT
    )
