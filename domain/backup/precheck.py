"""本番 backup 実行前の read-only 点検の判定（純粋。I/O は ``infrastructure/backup/precheck.py``）。

PASS / FAIL / UNKNOWN の 3 値。**確認できなかったものは UNKNOWN** で、PASS にしない。
総合判定は最悪の値（FAIL > UNKNOWN > PASS）。何も確認していなければ UNKNOWN。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

#: 必要容量 = 取得元の合計 × この係数（増分でない初回は全量。圧縮・一時ファイル・成長の余裕）
SAFETY_FACTOR = 2
#: 見積りの前提（実測ではなく保守的な目安。precheck の出力に前提として出す）
DB_DUMP_BYTES_PER_SEC = 40 * 1024 * 1024
OBJECT_COPY_BYTES_PER_SEC = 30 * 1024 * 1024
PER_OBJECT_OVERHEAD_SEC = 0.02
FIXED_OVERHEAD_SEC = 60
#: daily Schedule（cron は contracts/pipeline.py）の前後で backup を走らせない幅
DAILY_MARGIN_BEFORE = timedelta(minutes=15)
DAILY_MARGIN_AFTER = timedelta(hours=2)
#: watchdog（毎時 :35）の発火に対する開始時刻の距離
WATCHDOG_MARGIN = timedelta(minutes=3)

_DDL = re.compile(
    r"^\s*(ALTER|CREATE|DROP|TRUNCATE|VACUUM|REINDEX|CLUSTER|REFRESH)\b", re.IGNORECASE
)


class Status(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Check:
    id: str
    status: Status
    detail: str
    data: dict[str, Any] = field(default_factory=dict)


def overall(checks: Sequence[Check]) -> Status:
    if not checks:
        return Status.UNKNOWN
    statuses = {c.status for c in checks}
    if Status.FAIL in statuses:
        return Status.FAIL
    if Status.UNKNOWN in statuses:
        return Status.UNKNOWN
    return Status.PASS


def required_bytes(source_bytes: int) -> int:
    return source_bytes * SAFETY_FACTOR


def estimate(*, db_bytes: int, object_bytes: int, object_count: int) -> dict[str, Any]:
    seconds = (
        db_bytes / DB_DUMP_BYTES_PER_SEC
        + object_bytes / OBJECT_COPY_BYTES_PER_SEC
        + object_count * PER_OBJECT_OVERHEAD_SEC
        + FIXED_OVERHEAD_SEC
    )
    return {
        "seconds": round(seconds),
        "assumptions": {
            "db_dump_mb_per_sec": DB_DUMP_BYTES_PER_SEC // (1024 * 1024),
            "object_copy_mb_per_sec": OBJECT_COPY_BYTES_PER_SEC // (1024 * 1024),
            "per_object_overhead_sec": PER_OBJECT_OVERHEAD_SEC,
            "fixed_overhead_sec": FIXED_OVERHEAD_SEC,
        },
    }


def pg_major_from_dump_version(text: str) -> int | None:
    m = re.search(r"(\d+)(?:\.\d+)+\s*$", text.strip())
    return int(m.group(1)) if m else None


def pg_major_from_num(server_version_num: str) -> int | None:
    try:
        return int(server_version_num) // 10000
    except ValueError:
        return None


def ddl_like_sessions(rows: Sequence[Sequence[str]], *, min_seconds: float) -> list[dict[str, str]]:
    """``[pid, state, seconds, query]`` から、長く走っている DDL 系を返す。query 本文は返さない。"""
    found: list[dict[str, str]] = []
    for row in rows:
        if len(row) < 4:
            continue
        pid, state, secs, query = row[0], row[1], row[2], row[3]
        m = _DDL.match(query)
        try:
            long_running = float(secs) >= min_seconds
        except ValueError:
            long_running = True  # 読めない秒数は安全側
        if m and long_running:
            found.append(
                {"pid": pid, "state": state, "seconds": secs, "statement": m.group(1).upper()}
            )
    return found


def _daily_utc(cron: str, tz: str, day: datetime) -> datetime:
    fields = cron.split()
    if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
        raise ValueError(f"unsupported daily cron: {cron!r}")
    local = datetime.combine(day.date(), datetime.min.time(), tzinfo=ZoneInfo(tz)) + timedelta(
        hours=int(fields[1]), minutes=int(fields[0])
    )
    return local.astimezone(day.tzinfo)


def _watchdog_minute(cron: str) -> int:
    fields = cron.split()
    if len(fields) != 5 or fields[1:] != ["*", "*", "*", "*"]:
        raise ValueError(f"unsupported hourly cron: {cron!r}")
    return int(fields[0])


def schedule_conflicts(
    start: datetime, seconds: float, daily_cron: str, tz: str, watchdog_cron: str
) -> list[str]:
    """``[start, start+seconds]`` が daily / watchdog の窓と重なる理由。空なら重ならない。"""
    end = start + timedelta(seconds=seconds)
    out: list[str] = []
    for offset in (-1, 0, 1, 2):
        fire = _daily_utc(daily_cron, tz, start + timedelta(days=offset))
        if start < fire + DAILY_MARGIN_AFTER and end > fire - DAILY_MARGIN_BEFORE:
            out.append(f"overlaps daily schedule window around {fire.isoformat()}")
            break
    # watchdog は短い読み取り検査なので、実行中に発火すること自体は許す。
    # 開始の瞬間（PG の snapshot を取る時刻）が発火と重なるのだけ避ける
    minute = _watchdog_minute(watchdog_cron)
    t = start.replace(minute=0, second=0, microsecond=0) + timedelta(minutes=minute)
    for fire in (t - timedelta(hours=1), t, t + timedelta(hours=1)):
        if abs(start - fire) <= WATCHDOG_MARGIN:
            out.append(f"starts within watchdog fire window at {fire.isoformat()}")
            break
    return out


def next_safe_start(
    now: datetime, seconds: float, daily_cron: str, tz: str, watchdog_cron: str
) -> datetime | None:
    """now 以降で最初に窓と重ならない開始時刻（分刻みで 48 時間探す）。"""
    t = now.replace(second=0, microsecond=0)
    for _ in range(48 * 60):
        if not schedule_conflicts(t, seconds, daily_cron, tz, watchdog_cron):
            return t
        t += timedelta(minutes=1)
    return None
