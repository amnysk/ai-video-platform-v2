"""DailyWatchdogWorkflow の配線（ADR-0027）。検査本体は test_daily_watchdog.py。"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.schedule_guard import (
    WATCHDOG_CHECK_ACTIVITY,
    WatchdogCheckRequest,
    WatchdogRequest,
    WatchdogResult,
)
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.watchdog import DailyWatchdogWorkflow

SEEN: list[WatchdogCheckRequest] = []


@activity.defn(name=WATCHDOG_CHECK_ACTIVITY)
async def fake_check(request: WatchdogCheckRequest) -> WatchdogResult:
    SEEN.append(request)
    return WatchdogResult(schedule_health="healthy", daily_start="started_slot", slot_date="x")


@pytest.mark.asyncio
async def test_workflow_passes_its_own_clock_and_the_request_to_the_check_activity() -> None:
    SEEN.clear()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"watchdog-test-{uuid.uuid4()}"
        async with Worker(
            env.client, task_queue=queue, workflows=[DailyWatchdogWorkflow], activities=[fake_check]
        ):
            result = await env.client.execute_workflow(
                "DailyWatchdogWorkflow",
                WatchdogRequest(cron="0 6 * * *", timezone="Asia/Tokyo", grace_seconds=900),
                id=f"daily-watchdog-{uuid.uuid4()}",
                task_queue=queue,
                result_type=WatchdogResult,
            )
    assert result.daily_start == "started_slot"
    (seen,) = SEEN
    assert seen.grace_seconds == 900
    assert seen.timezone == "Asia/Tokyo"
    assert datetime.fromisoformat(seen.now).tzinfo is not None


def test_the_check_activity_is_registered_only_with_a_temporal_client(session_factory) -> None:
    without = PipelineActivities(
        session_factory=session_factory, paused_env=False, uploads_paused_env=False
    )
    with_client = PipelineActivities(
        session_factory=session_factory,
        paused_env=False,
        uploads_paused_env=False,
        temporal_client=object(),  # type: ignore[arg-type]
    )

    def names(acts: PipelineActivities) -> set[str]:
        return {fn.__temporal_activity_definition.name for fn in acts.activities()}  # type: ignore[attr-defined]

    assert WATCHDOG_CHECK_ACTIVITY not in names(without)
    assert WATCHDOG_CHECK_ACTIVITY in names(with_client)
