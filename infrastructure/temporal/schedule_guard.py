"""Daily Schedule の maintenance pause と reconcile（ADR-0027）。

規則（破らない）:

- 解除してよいのは **ガードの印（``MAINTENANCE_NOTE_PREFIX``）が有効な pause だけ**
- 印の無い pause（運用者の手動停止）は begin でも end でも reconcile でも**触らない**
- 解除したら describe で再確認し、paused=false・印が無い・次回実行が近い未来、を満たさなければ失敗

Temporal との接続は ``ScheduleControl``（``schedules.TemporalScheduleControl`` か fake）だけ。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from contracts.schedule_guard import (
    RELEASED_NOTE,
    MaintenanceMarker,
    ScheduleHealth,
)
from domain.schedule_guard import (
    build_maintenance_note,
    classify_schedule,
    maintenance_deadline,
)
from infrastructure.temporal.schedules import ScheduleSnapshot

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
#: 運用者の pause が有効なので何もしなかった。deploy は続けてよいが解除はしない
EXIT_EMERGENCY_PAUSED = 3

_VERIFY_ATTEMPTS = 3
_VERIFY_DELAY_SECONDS = 0.5

Sleep = Callable[[float], Awaitable[object]]


class ScheduleControl(Protocol):
    async def describe(self, schedule_id: str) -> ScheduleSnapshot: ...

    async def pause(self, schedule_id: str, note: str) -> None: ...

    async def unpause(self, schedule_id: str, note: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    exit_code: int
    health: ScheduleHealth
    message: str


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    health_before: ScheduleHealth
    health: ScheduleHealth
    released: bool


def classify_snapshot(snapshot: ScheduleSnapshot, now: datetime) -> ScheduleHealth:
    return classify_schedule(
        exists=snapshot.exists,
        paused=snapshot.paused,
        note=snapshot.note,
        next_run=snapshot.next_run,
        now=now,
    )


async def _verify_running(
    control: ScheduleControl, schedule_id: str, now: datetime, sleep: Sleep
) -> tuple[ScheduleHealth, ScheduleSnapshot]:
    """describe で「動いていて次回実行が妥当」を確かめる（反映の遅れに備えて少し再試行）。"""
    health = ScheduleHealth.MISSING
    snapshot = ScheduleSnapshot(exists=False)
    for attempt in range(_VERIFY_ATTEMPTS):
        snapshot = await control.describe(schedule_id)
        health = classify_snapshot(snapshot, now)
        if health is ScheduleHealth.HEALTHY:
            break
        if attempt + 1 < _VERIFY_ATTEMPTS:
            await sleep(_VERIFY_DELAY_SECONDS)
    return health, snapshot


async def begin_maintenance(
    control: ScheduleControl,
    schedule_id: str,
    *,
    reason: str,
    ttl_seconds: int,
    now: datetime,
    sleep: Sleep = asyncio.sleep,
) -> GuardOutcome:
    """maintenance pause を始める。期限は ``now + ttl``（上限あり。ValueError）。"""
    deadline = maintenance_deadline(now, ttl_seconds)  # 先に検証。Schedule に触る前に失敗させる
    snapshot = await control.describe(schedule_id)
    health = classify_snapshot(snapshot, now)
    if health is ScheduleHealth.MISSING:
        return GuardOutcome(EXIT_FAILED, health, f"schedule {schedule_id} does not exist")
    if health is ScheduleHealth.PAUSED_UNEXPECTEDLY:
        return GuardOutcome(
            EXIT_EMERGENCY_PAUSED,
            health,
            f"schedule {schedule_id} is paused without a maintenance marker "
            "(emergency pause); left untouched",
        )
    note = build_maintenance_note(MaintenanceMarker(reason=reason, deadline=deadline))
    await control.pause(schedule_id, note)
    after = await control.describe(schedule_id)
    after_health = classify_snapshot(after, now)
    if not after.paused or after_health is ScheduleHealth.MAINTENANCE_EXPIRED:
        return GuardOutcome(EXIT_FAILED, after_health, "pause did not take effect")
    if after_health is ScheduleHealth.PAUSED_UNEXPECTEDLY:
        # 直後に運用者が pause し直して note が置き換わった
        return GuardOutcome(EXIT_EMERGENCY_PAUSED, after_health, "taken over by an operator pause")
    return GuardOutcome(
        EXIT_OK, after_health, f"maintenance pause started (reason={reason}, deadline={deadline})"
    )


async def end_maintenance(
    control: ScheduleControl,
    schedule_id: str,
    *,
    now: datetime,
    sleep: Sleep = asyncio.sleep,
) -> GuardOutcome:
    """maintenance pause を解除し、動いていることを describe で確認する。冪等。"""
    snapshot = await control.describe(schedule_id)
    health = classify_snapshot(snapshot, now)
    if health is ScheduleHealth.MISSING:
        return GuardOutcome(EXIT_FAILED, health, f"schedule {schedule_id} does not exist")
    if health is ScheduleHealth.PAUSED_UNEXPECTEDLY:
        return GuardOutcome(
            EXIT_EMERGENCY_PAUSED,
            health,
            f"schedule {schedule_id} is paused by an operator (no maintenance marker); "
            "not unpausing",
        )
    if snapshot.paused:  # 有効または期限切れの maintenance pause
        await control.unpause(schedule_id, RELEASED_NOTE)
    verified, _ = await _verify_running(control, schedule_id, now, sleep)
    if verified is not ScheduleHealth.HEALTHY:
        return GuardOutcome(
            EXIT_FAILED,
            verified,
            f"schedule {schedule_id} is not healthy after release: {verified}",
        )
    return GuardOutcome(EXIT_OK, verified, f"schedule {schedule_id} is running")


async def reconcile_schedule(
    control: ScheduleControl,
    schedule_id: str,
    *,
    now: datetime,
    sleep: Sleep = asyncio.sleep,
) -> ReconcileOutcome:
    """期限切れの maintenance pause だけを解除する。ほかは観測して返すだけ（冪等）。"""
    snapshot = await control.describe(schedule_id)
    before = classify_snapshot(snapshot, now)
    if before is not ScheduleHealth.MAINTENANCE_EXPIRED:
        return ReconcileOutcome(before, before, released=False)
    logger.warning(
        "schedule maintenance overrun: releasing an expired maintenance pause schedule=%s",
        schedule_id,
    )
    await control.unpause(schedule_id, RELEASED_NOTE)
    after, _ = await _verify_running(control, schedule_id, now, sleep)
    return ReconcileOutcome(before, after, released=True)
