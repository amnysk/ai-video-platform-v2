"""Daily watchdog の本体（ADR-0027）。

毎日 06:00 の Schedule とは**別の Schedule**（``avp-daily-watchdog``、毎時）が起動する。
やること:

1. 期限切れの maintenance pause だけを解除する（emergency pause は触らない）
2. Schedule の状態を分類し、異常を DB へ記録する（``operational_anomalies``）
3. 予定時刻 + 猶予を過ぎても daily_episode_slot も DailyEpisodeWorkflow も無ければ
   ``DAILY_AUTOMATION_NOT_STARTED`` を記録する
4. まだ通知していない異常を ``AnomalyNotifier`` へ渡す（既定はログ。失敗は次回再送）

Schedule が pause されていても watchdog は動く（別 Schedule だから）。
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client

from contracts.operations import OperationalSwitch
from contracts.schedule_guard import (
    HEALTH_ANOMALY,
    AnomalyKind,
    DailyStartStatus,
    ScheduleHealth,
    WatchdogCheckRequest,
    WatchdogResult,
)
from domain.schedule_guard import daily_start_status, latest_daily_fire
from infrastructure.db.repositories import (
    AnomalyRecord,
    DailyEpisodeSlotRepository,
    OperationalAnomalyRepository,
    OperationalSwitchRepository,
)
from infrastructure.observability.anomaly_notifier import AnomalyNotice, AnomalyNotifier
from infrastructure.temporal.schedule_guard import ScheduleControl, reconcile_schedule

logger = logging.getLogger(__name__)

#: 健全なら閉じてよい Schedule 系の異常
_SCHEDULE_KINDS = (
    AnomalyKind.SCHEDULE_PAUSED_UNEXPECTEDLY,
    AnomalyKind.SCHEDULE_MISSING,
    AnomalyKind.SCHEDULE_NEXT_RUN_INVALID,
)


class WorkflowStartCounter(Protocol):
    async def count_started_since(self, workflow_type: str, since: datetime) -> int: ...


class TemporalWorkflowStartCounter:
    """Temporal の visibility で、``since`` 以降に始まった workflow の数を数える。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def count_started_since(self, workflow_type: str, since: datetime) -> int:
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = f'WorkflowType = "{workflow_type}" AND StartTime >= "{stamp}"'
        return (await self._client.count_workflows(query)).count


async def run_daily_watchdog(
    *,
    control: ScheduleControl,
    session_factory: async_sessionmaker[AsyncSession],
    workflow_counter: WorkflowStartCounter,
    notifier: AnomalyNotifier,
    request: WatchdogCheckRequest,
) -> WatchdogResult:
    now = datetime.fromisoformat(request.now)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local_today = _local_date(now, request.timezone)

    reconciled = await reconcile_schedule(control, request.schedule_id, now=now)
    health = reconciled.health
    detail_base: dict[str, object] = {
        "schedule_id": request.schedule_id,
        "schedule_health": health.value,
    }

    daily_status = DailyStartStatus.UNSUPPORTED_CRON
    slot_date: date | None = None
    workflow_query = "ok"
    try:
        slot_date, fire_time = latest_daily_fire(now, request.cron, request.timezone)
    except ValueError:
        logger.warning(
            "watchdog: cron %r is not a plain daily cron; start check skipped", request.cron
        )
    else:
        async with session_factory() as session:
            has_slot = bool(await DailyEpisodeSlotRepository(session).list_for_date(slot_date))
        has_workflow = False
        if not has_slot:
            try:
                has_workflow = (
                    await workflow_counter.count_started_since(request.workflow_type, fire_time) > 0
                )
            except Exception as exc:  # visibility 障害で「未起動」を隠さない（slot が主）
                workflow_query = "failed"
                logger.warning("watchdog: workflow visibility query failed: %s", type(exc).__name__)
        daily_status = daily_start_status(
            now=now,
            fire_time=fire_time,
            grace_seconds=request.grace_seconds,
            has_slot=has_slot,
            has_workflow=has_workflow,
        )
        detail_base["expected_fire_time"] = fire_time.isoformat()

    new_records: list[AnomalyRecord] = []
    async with session_factory() as session:
        anomalies = OperationalAnomalyRepository(session)
        switches = OperationalSwitchRepository(session)
        detail_base["switch_paused"] = await switches.is_on(OperationalSwitch.PAUSED)
        detail_base["switch_uploads_paused"] = await switches.is_on(
            OperationalSwitch.UPLOADS_PAUSED
        )

        if reconciled.released:
            new_records.append(
                await anomalies.record(
                    AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN,
                    local_today,
                    {**detail_base, "note": "expired maintenance pause released by the guard"},
                    now=now,
                )
            )
        kind = HEALTH_ANOMALY[health]
        if kind is not None:
            new_records.append(
                await anomalies.record(kind, local_today, dict(detail_base), now=now)
            )
        elif health is ScheduleHealth.HEALTHY:
            await anomalies.resolve_open(_SCHEDULE_KINDS, now=now)
            if not reconciled.released:
                await anomalies.resolve_open([AnomalyKind.SCHEDULE_MAINTENANCE_OVERRUN], now=now)

        if slot_date is not None:
            if daily_status is DailyStartStatus.NOT_STARTED:
                new_records.append(
                    await anomalies.record(
                        AnomalyKind.DAILY_AUTOMATION_NOT_STARTED,
                        slot_date,
                        {
                            **detail_base,
                            "slot_date": slot_date.isoformat(),
                            "workflow_query": workflow_query,
                        },
                        now=now,
                    )
                )
            elif daily_status in (DailyStartStatus.STARTED_SLOT, DailyStartStatus.STARTED_WORKFLOW):
                await anomalies.resolve(
                    AnomalyKind.DAILY_AUTOMATION_NOT_STARTED, slot_date, now=now
                )

        for record in new_records:
            if record.is_new:
                logger.error(
                    "anomaly=%s date=%s (recorded)", record.kind, record.anomaly_date.isoformat()
                )
        await session.commit()

        for pending in await anomalies.pending_notifications():
            try:
                await notifier.notify(
                    AnomalyNotice(
                        kind=AnomalyKind(pending.kind),
                        anomaly_date=pending.anomaly_date,
                        occurrences=pending.occurrences,
                        detail=pending.detail,
                    )
                )
            except Exception as exc:  # 通知の失敗は検査を止めない。notified_at が空なので次回再送
                logger.warning("watchdog: notifier failed: %s", type(exc).__name__)
                continue
            await anomalies.mark_notified(pending.id, now=now)
        await session.commit()

    return WatchdogResult(
        schedule_health=health.value,
        daily_start=daily_status.value,
        anomalies=[r.kind for r in new_records if r.is_new],
        released_maintenance=reconciled.released,
        slot_date=slot_date.isoformat() if slot_date else "",
    )


def _local_date(now: datetime, timezone: str) -> date:
    from zoneinfo import ZoneInfo

    return now.astimezone(ZoneInfo(timezone)).date()
