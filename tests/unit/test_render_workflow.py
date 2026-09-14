"""RenderWorkflow の編成（ADR-0019）。time-skipping テストサーバ + 名前で登録した mock Activity。

Activity の実装は import しない（INV-3）。順序・retry 上限・失敗クラスの写像・cancel だけを検査する。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.api.enums.v1 import RetryState, TimeoutType
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.render import DEFAULT_RENDER_TIMEOUT_SECONDS
from contracts.render_activities import (
    RENDER_ADMIT,
    RENDER_FINAL_VIDEO,
    RENDER_MARK_READY,
    RENDER_MAX_ATTEMPTS,
    RENDER_RECORD_FAILURE,
    RenderAdmitRequest,
    RenderAdmitResult,
    RenderFailureOutcome,
    RenderFinalVideoRequest,
    RenderFinalVideoResult,
    RenderMarkReadyRequest,
    RenderMarkReadyResult,
    RenderRecordFailureRequest,
)
from contracts.states import EpisodeStatus, FailureClass
from workers.render.workflows import (
    RENDER_ACTIVITY_MARGIN_SECONDS,
    RenderWorkflow,
    RenderWorkflowInput,
    classify_render_failure,
    render_start_to_close,
)


@dataclass
class Mocks:
    admitted: bool = True
    calls: list[str] = field(default_factory=list)
    render_requests: list[RenderFinalVideoRequest] = field(default_factory=list)
    render_errors: list[ApplicationError] = field(default_factory=list)
    always_fail: ApplicationError | None = None
    failures: list[RenderRecordFailureRequest] = field(default_factory=list)
    hang: bool = False
    cancelled: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)

    def activities(self) -> list[Any]:
        @activity.defn(name=RENDER_ADMIT)
        async def admit(req: RenderAdmitRequest) -> RenderAdmitResult:
            self.calls.append("admit")
            if self.admitted:
                return RenderAdmitResult(admitted=True, status="in_progress")
            return RenderAdmitResult(admitted=False, status="storyboard_ready")

        @activity.defn(name=RENDER_FINAL_VIDEO)
        async def render(req: RenderFinalVideoRequest) -> RenderFinalVideoResult:
            self.calls.append("render")
            self.render_requests.append(req)
            self.started.set()
            if self.hang:
                try:
                    while True:
                        activity.heartbeat()
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    self.cancelled = True
                    self.calls.append("render_cancelled")
                    raise
            if self.always_fail is not None:
                raise self.always_fail
            if self.render_errors:
                raise self.render_errors.pop(0)
            return RenderFinalVideoResult(
                artifact_id=str(uuid.uuid4()), sha256="a" * 64, version=1, skipped=False
            )

        @activity.defn(name=RENDER_MARK_READY)
        async def mark_ready(req: RenderMarkReadyRequest) -> RenderMarkReadyResult:
            self.calls.append("mark_ready")
            return RenderMarkReadyResult(status=EpisodeStatus.RENDER_READY.value)

        @activity.defn(name=RENDER_RECORD_FAILURE)
        async def record_failure(req: RenderRecordFailureRequest) -> RenderFailureOutcome:
            self.calls.append("record_failure")
            self.failures.append(req)
            status = {
                FailureClass.PERMANENT.value: "failed",
                FailureClass.RETRYABLE.value: "blocked" if req.retry_exhausted else "needs_work",
            }.get(req.failure_class, "blocked")
            return RenderFailureOutcome(episode_status=status)

        return [admit, render, mark_ready, record_failure]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _error(type_name: str, *, non_retryable: bool = False) -> ApplicationError:
    return ApplicationError(f"{type_name}: boom", type=type_name, non_retryable=non_retryable)


async def _run(env: WorkflowEnvironment, mocks: Mocks, *, cancel: bool = False, **kw: Any):
    queue = f"render-test-{uuid.uuid4().hex[:10]}"
    async with Worker(
        env.client,
        task_queue=queue,
        workflows=[RenderWorkflow],
        activities=mocks.activities(),
        # cancel を heartbeat の応答ですぐ受け取る
        max_heartbeat_throttle_interval=timedelta(milliseconds=50),
        default_heartbeat_throttle_interval=timedelta(milliseconds=50),
    ):
        handle = await env.client.start_workflow(
            RenderWorkflow.run,
            RenderWorkflowInput(episode_id="ep-1", **kw),
            id=f"episode-ep-1-render-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
        if cancel:
            await asyncio.wait_for(mocks.started.wait(), timeout=10)
            await handle.cancel()
        return await asyncio.wait_for(handle.result(), timeout=60)


async def test_happy_path_admits_renders_and_marks_ready(env) -> None:
    mocks = Mocks()

    result = await _run(env, mocks, render_profile_id="long_form_horizontal")

    assert mocks.calls == ["admit", "render", "mark_ready"]
    assert result.status == EpisodeStatus.RENDER_READY.value
    assert result.final_video is not None and result.final_video.version == 1
    (req,) = mocks.render_requests
    assert req.render_profile_id == "long_form_horizontal" and req.run_id


async def test_not_admitted_does_nothing(env) -> None:
    mocks = Mocks(admitted=False)

    result = await _run(env, mocks)

    assert mocks.calls == ["admit"]
    assert not result.admitted and result.status == "storyboard_ready"


async def test_transient_render_failure_is_retried_by_temporal(env) -> None:
    mocks = Mocks(render_errors=[_error("RenderEngineFailedError")])

    result = await _run(env, mocks)

    assert mocks.calls == ["admit", "render", "render", "mark_ready"]
    assert result.status == EpisodeStatus.RENDER_READY.value


async def test_retry_exhaustion_blocks_and_never_fails_terminally(env) -> None:
    mocks = Mocks(always_fail=_error("RenderEngineFailedError"))

    result = await _run(env, mocks)

    assert mocks.calls.count("render") == RENDER_MAX_ATTEMPTS
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.RETRYABLE.value and failure.retry_exhausted
    assert result.status == "blocked" and result.failure_class == "retryable"


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("RenderInputMissingError", FailureClass.NEEDS_INPUT),
        ("DurationReconciliationError", FailureClass.NEEDS_INPUT),
        ("RenderInputIntegrityError", FailureClass.PERMANENT),
        ("UnknownRenderProfileError", FailureClass.PERMANENT),
        ("SomethingUnclassified", FailureClass.NEEDS_INPUT),
    ],
)
async def test_non_retryable_failures_are_recorded_once_with_their_class(
    env, type_name, expected
) -> None:
    mocks = Mocks(always_fail=_error(type_name, non_retryable=True))

    await _run(env, mocks)

    assert mocks.calls == ["admit", "render", "record_failure"]
    (failure,) = mocks.failures
    assert failure.failure_class == expected.value and not failure.retry_exhausted


async def test_cancel_waits_for_the_render_to_stop_then_records_and_reraises(env) -> None:
    mocks = Mocks(hang=True)

    with pytest.raises(WorkflowFailureError) as info:
        await _run(env, mocks, cancel=True)

    assert isinstance(info.value.cause, CancelledError)
    assert mocks.cancelled
    assert mocks.calls[-2:] == ["render_cancelled", "record_failure"]
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value
    assert "cancelled" in failure.error_summary


def test_render_start_to_close_leaves_margin_over_the_engine_timeout() -> None:
    assert render_start_to_close(DEFAULT_RENDER_TIMEOUT_SECONDS) == timedelta(
        seconds=DEFAULT_RENDER_TIMEOUT_SECONDS + RENDER_ACTIVITY_MARGIN_SECONDS
    )


def test_temporal_timeouts_count_as_retryable() -> None:
    err = ActivityError(
        "activity timed out",
        scheduled_event_id=1,
        started_event_id=2,
        identity="w",
        activity_type=RENDER_FINAL_VIDEO,
        activity_id="1",
        retry_state=RetryState.RETRY_STATE_MAXIMUM_ATTEMPTS_REACHED,
    )
    err.__cause__ = TemporalTimeoutError(
        "heartbeat", type=TimeoutType.TIMEOUT_TYPE_HEARTBEAT, last_heartbeat_details=[]
    )

    assert classify_render_failure(err) is FailureClass.RETRYABLE
