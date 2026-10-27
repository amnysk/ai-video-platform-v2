"""``TemporalDailyRunReader``: visibility 第一、Schedule 第二、両方だめなら例外（ADR-0054）。

Temporal client は fake。実 Temporal・本番には接続しない。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from temporalio.client import WorkflowExecutionStatus, WorkflowFailureError
from temporalio.exceptions import ApplicationError

from contracts.schedule_guard import DailyRunStatus
from infrastructure.temporal.daily_runs import TemporalDailyRunReader, map_status

SINCE = datetime(2026, 9, 19, 15, 0, tzinfo=UTC)
START = datetime(2026, 9, 20, 21, 0, 1, tzinfo=UTC)  # 06:00:01 JST = 9/21


def _info(wid: str, status: WorkflowExecutionStatus) -> Any:
    return SimpleNamespace(id=wid, run_id="r1", status=status, start_time=START)


class _Handle:
    def __init__(self, info: Any = None, failure: BaseException | None = None) -> None:
        self._info = info
        self._failure = failure

    async def result(self) -> None:
        if self._failure is not None:
            raise self._failure

    async def describe(self) -> Any:
        if self._info is None:
            raise RuntimeError("workflow not found (retention)")
        return self._info


class _Client:
    def __init__(
        self,
        listed: list[Any] | None = None,
        *,
        list_error: bool = False,
        recent: list[Any] | None = None,
        schedule_error: bool = False,
        handles: dict[str, _Handle] | None = None,
    ) -> None:
        self.listed = listed or []
        self.list_error = list_error
        self.recent = recent or []
        self.schedule_error = schedule_error
        self.handles = handles or {}
        self.queries: list[str] = []

    async def list_workflows(self, query: str):  # type: ignore[no-untyped-def]
        self.queries.append(query)
        if self.list_error:
            raise RuntimeError("visibility down")
        for info in self.listed:
            yield info

    def get_schedule_handle(self, schedule_id: str) -> Any:
        async def describe() -> Any:
            if self.schedule_error:
                raise RuntimeError("schedule unreadable")
            return SimpleNamespace(info=SimpleNamespace(recent_actions=self.recent))

        return SimpleNamespace(describe=describe)

    def get_workflow_handle(self, workflow_id: str, run_id: str | None = None) -> _Handle:
        return self.handles.get(workflow_id, _Handle())


def _failure_error() -> WorkflowFailureError:
    err = WorkflowFailureError(cause=ApplicationError("api_key=sk-live-ABCDEF123456 exploded"))
    return err


async def _read(client: _Client):  # type: ignore[no-untyped-def]
    reader = TemporalDailyRunReader(client)  # type: ignore[arg-type]
    return await reader.read_runs(
        workflow_type="DailyEpisodeWorkflow",
        schedule_id="avp-daily-episode",
        since=SINCE,
        timezone="Asia/Tokyo",
    )


def test_status_mapping_covers_every_temporal_status() -> None:
    assert {s: map_status(s) for s in WorkflowExecutionStatus} == {
        WorkflowExecutionStatus.RUNNING: DailyRunStatus.RUNNING,
        WorkflowExecutionStatus.COMPLETED: DailyRunStatus.COMPLETED,
        WorkflowExecutionStatus.FAILED: DailyRunStatus.FAILED,
        WorkflowExecutionStatus.CANCELED: DailyRunStatus.CANCELED,
        WorkflowExecutionStatus.TERMINATED: DailyRunStatus.TERMINATED,
        WorkflowExecutionStatus.CONTINUED_AS_NEW: DailyRunStatus.CONTINUED_AS_NEW,
        WorkflowExecutionStatus.TIMED_OUT: DailyRunStatus.TIMED_OUT,
    }
    assert map_status(None) is DailyRunStatus.OTHER


async def test_visibility_is_primary_and_failure_cause_is_summarized_without_secrets() -> None:
    client = _Client(
        [_info("daily-episode-a", WorkflowExecutionStatus.FAILED)],
        handles={"daily-episode-a": _Handle(failure=_failure_error())},
    )
    (run,) = await _read(client)
    assert run.slot_date == date(2026, 9, 21)
    assert run.status is DailyRunStatus.FAILED
    assert run.failure_type == "ApplicationError"
    assert run.failure_message is not None and "sk-live" not in run.failure_message
    assert 'StartTime >= "2026-09-19T15:00:00Z"' in client.queries[0]


async def test_failure_without_a_readable_cause_is_still_failed() -> None:
    client = _Client([_info("daily-episode-a", WorkflowExecutionStatus.FAILED)])
    (run,) = await _read(client)
    assert run.status is DailyRunStatus.FAILED
    assert (run.failure_type, run.failure_message) == (None, None)


async def test_falls_back_to_schedule_actions_and_skips_expired_executions() -> None:
    recent = [
        SimpleNamespace(started_at=START, action=SimpleNamespace(workflow_id="kept")),
        SimpleNamespace(started_at=START, action=SimpleNamespace(workflow_id="expired")),
        SimpleNamespace(
            started_at=datetime(2026, 9, 1, tzinfo=UTC), action=SimpleNamespace(workflow_id="old")
        ),
    ]
    client = _Client(
        list_error=True,
        recent=recent,
        handles={"kept": _Handle(_info("kept", WorkflowExecutionStatus.TERMINATED))},
    )
    runs = await _read(client)
    assert [(r.workflow_id, r.status) for r in runs] == [("kept", DailyRunStatus.TERMINATED)]


async def test_both_sources_unreadable_raises_so_the_watchdog_can_record_unobservable() -> None:
    with pytest.raises(RuntimeError, match="unreadable"):
        await _read(_Client(list_error=True, schedule_error=True))
