"""DailyWatchdogWorkflow（ADR-0027）。I/O をしない。

Schedule ``avp-daily-watchdog``（毎時）が起動し、検査を Activity 1つに任せる。時刻は workflow の
``now``（決定論）を渡す。検査が落ちても翌時の Schedule が再度検査するので、再試行は短くて足りる。
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from contracts.schedule_guard import (
        WATCHDOG_CHECK_ACTIVITY,
        WatchdogCheckRequest,
        WatchdogRequest,
        WatchdogResult,
    )

_CHECK_TIMEOUT = timedelta(minutes=2)
_CHECK_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=4,
)


@workflow.defn(name="DailyWatchdogWorkflow")
class DailyWatchdogWorkflow:
    @workflow.run
    async def run(self, request: WatchdogRequest) -> WatchdogResult:
        return await workflow.execute_activity(
            WATCHDOG_CHECK_ACTIVITY,
            WatchdogCheckRequest(
                now=workflow.now().isoformat(),
                schedule_id=request.schedule_id,
                cron=request.cron,
                timezone=request.timezone,
                grace_seconds=request.grace_seconds,
                workflow_type=request.workflow_type,
                pipeline_workflow_type=request.pipeline_workflow_type,
                stage_stall_grace_minutes=request.stage_stall_grace_minutes,
                blocked_grace_minutes=request.blocked_grace_minutes,
                completion_deadline_hours=request.completion_deadline_hours,
                upload_deadline_hours=request.upload_deadline_hours,
            ),
            result_type=WatchdogResult,
            start_to_close_timeout=_CHECK_TIMEOUT,
            retry_policy=_CHECK_RETRY,
        )
