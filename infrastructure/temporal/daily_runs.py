"""日次 workflow の終了状態を Temporal から読む（読み取りだけ。ADR-0054）。

第一に visibility（``list_workflows``）、読めなければ Schedule の ``recent_actions`` から
workflow id を拾って各 ``describe``。両方読めなければ例外を投げ、watchdog が UNOBSERVABLE にする。
失敗の理由は ``handle.result()`` が投げる cause 連鎖の末端から取り、``summarize_failure`` で
secret を伏せて短くしてから返す。stack trace・入力 payload は取らない。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from temporalio.client import Client, WorkflowExecutionStatus, WorkflowFailureError

from contracts.schedule_guard import DailyRunStatus
from domain.daily_failure import DailyRunObservation, slot_date_of, summarize_failure
from infrastructure.temporal.schedules import recent_action_workflow_ids

logger = logging.getLogger(__name__)

_STATUS: dict[WorkflowExecutionStatus, DailyRunStatus] = {
    WorkflowExecutionStatus.RUNNING: DailyRunStatus.RUNNING,
    WorkflowExecutionStatus.COMPLETED: DailyRunStatus.COMPLETED,
    WorkflowExecutionStatus.CONTINUED_AS_NEW: DailyRunStatus.CONTINUED_AS_NEW,
    WorkflowExecutionStatus.FAILED: DailyRunStatus.FAILED,
    WorkflowExecutionStatus.TIMED_OUT: DailyRunStatus.TIMED_OUT,
    WorkflowExecutionStatus.TERMINATED: DailyRunStatus.TERMINATED,
    WorkflowExecutionStatus.CANCELED: DailyRunStatus.CANCELED,
}


def map_status(status: WorkflowExecutionStatus | None) -> DailyRunStatus:
    return _STATUS.get(status, DailyRunStatus.OTHER) if status is not None else DailyRunStatus.OTHER


class TemporalDailyRunReader:
    def __init__(self, client: Client) -> None:
        self._client = client

    async def read_runs(
        self, *, workflow_type: str, schedule_id: str, since: datetime, timezone: str
    ) -> list[DailyRunObservation]:
        try:
            return await self._from_visibility(workflow_type, since, timezone)
        except Exception as primary:
            logger.warning(
                "watchdog: visibility read of daily runs failed (%s); trying schedule actions",
                type(primary).__name__,
            )
            try:
                return await self._from_schedule(schedule_id, since, timezone)
            except Exception as fallback:
                raise RuntimeError(
                    f"daily runs unreadable: visibility={type(primary).__name__}, "
                    f"schedule={type(fallback).__name__}"
                ) from fallback

    async def _from_visibility(
        self, workflow_type: str, since: datetime, timezone: str
    ) -> list[DailyRunObservation]:
        stamp = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = f'WorkflowType = "{workflow_type}" AND StartTime >= "{stamp}"'
        runs: list[DailyRunObservation] = []
        async for info in self._client.list_workflows(query):
            if info.start_time is None:
                continue
            runs.append(
                await self._observe(info.id, info.run_id, info.status, info.start_time, timezone)
            )
        return runs

    async def _from_schedule(
        self, schedule_id: str, since: datetime, timezone: str
    ) -> list[DailyRunObservation]:
        runs: list[DailyRunObservation] = []
        for workflow_id in await recent_action_workflow_ids(self._client, schedule_id, since):
            try:  # 履歴が保持期間で消えていれば、この実行は「見つからない」扱い
                info = await self._client.get_workflow_handle(workflow_id).describe()
            except Exception:
                continue
            if info.start_time is None:
                continue
            runs.append(
                await self._observe(info.id, info.run_id, info.status, info.start_time, timezone)
            )
        return runs

    async def _observe(
        self,
        workflow_id: str,
        run_id: str | None,
        raw_status: WorkflowExecutionStatus | None,
        start_time: datetime,
        timezone: str,
    ) -> DailyRunObservation:
        status = map_status(raw_status)
        failure_type: str | None = None
        failure_message: str | None = None
        if status is DailyRunStatus.FAILED:
            failure_type, failure_message = await self._failure_of(workflow_id, run_id)
        kind, message = summarize_failure(failure_type, failure_message)
        return DailyRunObservation(
            slot_date=slot_date_of(start_time, timezone),
            workflow_id=workflow_id,
            status=status,
            started_at=start_time,
            failure_type=kind,
            failure_message=message,
        )

    async def _failure_of(
        self, workflow_id: str, run_id: str | None
    ) -> tuple[str | None, str | None]:
        handle = self._client.get_workflow_handle(workflow_id, run_id=run_id)
        try:
            await handle.result()
        except WorkflowFailureError as exc:
            root: BaseException = exc
            while root.__cause__ is not None:
                root = root.__cause__
            return type(root).__name__, str(root)
        except Exception as exc:  # 理由が取れなくても FAILED は FAILED として残す
            logger.warning(
                "watchdog: could not read failure of %s: %s", workflow_id, type(exc).__name__
            )
        return None, None
