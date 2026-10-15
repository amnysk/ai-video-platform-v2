"""``TemporalAgenticStarter.probe_workers``（ADR-0044 §3-3）。各テストの理由:
docs/testing/jpmas2-p2a-fixes-rationale.md
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from temporalio.api.enums.v1 import TaskQueueType
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.agentic_pipeline import AGENTS_TASK_QUEUE
from contracts.application import WorkerCheck
from contracts.pipeline import PipelineOptions
from infrastructure.temporal.agentic_starter import TemporalAgenticStarter


class _Response:
    def __init__(self, pollers: int) -> None:
        self.pollers = [object()] * pollers


class _Service:
    def __init__(self, pollers: dict[tuple[str, int], int | Exception]) -> None:
        self._pollers = pollers
        self.requests: list[tuple[str, int]] = []

    async def describe_task_queue(self, request: Any, **_: Any) -> _Response:
        key = (request.task_queue.name, request.task_queue_type)
        self.requests.append(key)
        value = self._pollers[key]
        if isinstance(value, Exception):
            raise value
        return _Response(value)


class _Client:
    namespace = "default"

    def __init__(self, service: _Service, *, already_started: bool = False) -> None:
        self.workflow_service = service
        self._already_started = already_started
        self.started: list[str] = []

    async def start_workflow(self, name: str, arg: Any, *, id: str, **_: Any) -> None:  # noqa: A002
        if self._already_started:
            raise WorkflowAlreadyStartedError(id, name, run_id="r")
        self.started.append(id)


WF = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW
ACT = TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY


def _starter(
    pollers: dict[tuple[str, int], int | Exception], **kw: Any
) -> tuple[TemporalAgenticStarter, _Client]:
    client = _Client(_Service(pollers), **kw)
    options = PipelineOptions(pipeline_task_queue="custom-pipeline")
    return TemporalAgenticStarter(client, options), client  # type: ignore[arg-type]


async def test_both_queues_with_pollers_is_pollers_present() -> None:
    starter, client = _starter({("custom-pipeline", WF): 1, (AGENTS_TASK_QUEUE, ACT): 2})
    assert await starter.probe_workers() is WorkerCheck.POLLERS_PRESENT
    # 設定の pipeline queue と agents の activity queue を見ている
    assert sorted(client.workflow_service.requests) == sorted(
        [("custom-pipeline", WF), (AGENTS_TASK_QUEUE, ACT)]
    )


@pytest.mark.parametrize(
    "pollers",
    [
        {("custom-pipeline", WF): 0, (AGENTS_TASK_QUEUE, ACT): 1},
        {("custom-pipeline", WF): 1, (AGENTS_TASK_QUEUE, ACT): 0},
        {("custom-pipeline", WF): 0, (AGENTS_TASK_QUEUE, ACT): RuntimeError("x")},
    ],
)
async def test_a_queue_with_no_pollers_is_no_pollers(pollers: Any) -> None:
    starter, _ = _starter(pollers)
    assert await starter.probe_workers() is WorkerCheck.NO_POLLERS


async def test_any_error_without_a_definite_answer_is_unknown() -> None:
    starter, _ = _starter(
        {("custom-pipeline", WF): RuntimeError("down"), (AGENTS_TASK_QUEUE, ACT): 1}
    )
    assert await starter.probe_workers() is WorkerCheck.UNKNOWN


async def test_a_client_without_a_service_is_unknown_never_raises() -> None:
    starter = TemporalAgenticStarter(object(), PipelineOptions())  # type: ignore[arg-type]
    assert await starter.probe_workers() is WorkerCheck.UNKNOWN


async def test_start_reports_the_worker_check_and_survives_a_failing_probe() -> None:
    starter, client = _starter({("custom-pipeline", WF): 0, (AGENTS_TASK_QUEUE, ACT): 0})
    started = await starter.start(episode_id=uuid.uuid4(), template_id="t", template_version="1")
    assert started.started is True and started.worker_check is WorkerCheck.NO_POLLERS

    broken, _ = _starter(
        {("custom-pipeline", WF): RuntimeError("x"), (AGENTS_TASK_QUEUE, ACT): RuntimeError("y")}
    )
    result = await broken.start(episode_id=uuid.uuid4(), template_id="t", template_version="1")
    assert result.started is True and result.worker_check is WorkerCheck.UNKNOWN


async def test_an_already_started_workflow_still_reports_the_check() -> None:
    starter, _ = _starter(
        {("custom-pipeline", WF): 1, (AGENTS_TASK_QUEUE, ACT): 1}, already_started=True
    )
    result = await starter.start(episode_id=uuid.uuid4(), template_id="t", template_version="1")
    assert result.started is False and result.worker_check is WorkerCheck.POLLERS_PRESENT
