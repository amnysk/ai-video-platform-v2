"""UploadWorkflow の編成（ADR-0020）。time-skipping テストサーバ + 名前で登録した mock Activity。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError, CancelledError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.states import UPLOAD_MEDIA_TASK_QUEUE, EpisodeStatus, FailureClass
from contracts.upload import DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS
from contracts.upload_activities import (
    UPLOAD_ADMIT,
    UPLOAD_AWAIT_PROCESSING,
    UPLOAD_FINAL_VIDEO,
    UPLOAD_MARK_UPLOADED,
    UPLOAD_MAX_ATTEMPTS,
    UPLOAD_RECORD_FAILURE,
    UploadAdmitRequest,
    UploadAdmitResult,
    UploadAwaitProcessingRequest,
    UploadAwaitProcessingResult,
    UploadFailureOutcome,
    UploadFinalVideoRequest,
    UploadFinalVideoResult,
    UploadMarkUploadedRequest,
    UploadMarkUploadedResult,
    UploadRecordFailureRequest,
)
from workers.upload.workflows import (
    PROCESSING_RETRY_POLICY,
    PROCESSING_SCHEDULE_TO_CLOSE,
    UPLOAD_ACTIVITY_MARGIN_SECONDS,
    UploadWorkflow,
    UploadWorkflowInput,
    upload_start_to_close,
)


@dataclass
class Mocks:
    admitted: bool = True
    calls: list[str] = field(default_factory=list)
    errors: list[ApplicationError] = field(default_factory=list)
    always_fail: ApplicationError | None = None
    failures: list[UploadRecordFailureRequest] = field(default_factory=list)
    hang: bool = False
    cancelled: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    processing_errors: list[ApplicationError] = field(default_factory=list)
    processing_always_fail: ApplicationError | None = None
    processing_requests: list[UploadAwaitProcessingRequest] = field(default_factory=list)

    def activities(self) -> list[Any]:
        @activity.defn(name=UPLOAD_ADMIT)
        async def admit(req: UploadAdmitRequest) -> UploadAdmitResult:
            self.calls.append("admit")
            if self.admitted:
                return UploadAdmitResult(admitted=True, status="in_progress")
            return UploadAdmitResult(admitted=False, status="uploaded")

        @activity.defn(name=UPLOAD_FINAL_VIDEO)
        async def upload(req: UploadFinalVideoRequest) -> UploadFinalVideoResult:
            self.calls.append("upload")
            self.started.set()
            if self.hang:
                try:
                    while True:
                        activity.heartbeat()
                        await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    self.cancelled = True
                    self.calls.append("upload_cancelled")
                    raise
            if self.always_fail is not None:
                raise self.always_fail
            if self.errors:
                raise self.errors.pop(0)
            return UploadFinalVideoResult(
                artifact_id=str(uuid.uuid4()),
                sha256="a" * 64,
                version=1,
                video_id="vid00000001",
                skipped=False,
                reconciled_by="upload_response",
            )

        @activity.defn(name=UPLOAD_AWAIT_PROCESSING)
        async def processing(req: UploadAwaitProcessingRequest) -> UploadAwaitProcessingResult:
            self.calls.append("processing")
            self.processing_requests.append(req)
            if self.processing_always_fail is not None:
                raise self.processing_always_fail
            if self.processing_errors:
                raise self.processing_errors.pop(0)
            return UploadAwaitProcessingResult(
                video_id=req.video_id, upload_status="processed", reason="processed"
            )

        @activity.defn(name=UPLOAD_MARK_UPLOADED)
        async def mark(req: UploadMarkUploadedRequest) -> UploadMarkUploadedResult:
            self.calls.append("mark_uploaded")
            return UploadMarkUploadedResult(status=EpisodeStatus.UPLOADED.value)

        @activity.defn(name=UPLOAD_RECORD_FAILURE)
        async def record_failure(req: UploadRecordFailureRequest) -> UploadFailureOutcome:
            self.calls.append("record_failure")
            self.failures.append(req)
            status = {
                FailureClass.PERMANENT.value: "failed",
                FailureClass.RETRYABLE.value: "blocked" if req.retry_exhausted else "needs_work",
                FailureClass.TRANSIENT.value: "blocked" if req.retry_exhausted else "needs_work",
            }.get(req.failure_class, "blocked")
            return UploadFailureOutcome(episode_status=status)

        return [admit, upload, processing, mark, record_failure]


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _error(type_name: str, *, non_retryable: bool = False) -> ApplicationError:
    return ApplicationError(
        f"{type_name}: boom", "job-1", type=type_name, non_retryable=non_retryable
    )


async def _run(env: WorkflowEnvironment, mocks: Mocks, *, cancel: bool = False):
    queue = f"upload-test-{uuid.uuid4().hex[:10]}"
    media_queue = f"upload-media-test-{uuid.uuid4().hex[:10]}"
    acts = mocks.activities()
    state_names = {
        UPLOAD_ADMIT,
        UPLOAD_AWAIT_PROCESSING,
        UPLOAD_MARK_UPLOADED,
        UPLOAD_RECORD_FAILURE,
    }
    async with (
        Worker(
            env.client,
            task_queue=queue,
            workflows=[UploadWorkflow],
            activities=[a for a in acts if a.__temporal_activity_definition.name in state_names],
        ),
        Worker(
            env.client,
            task_queue=media_queue,
            activities=[
                a for a in acts if a.__temporal_activity_definition.name == UPLOAD_FINAL_VIDEO
            ],
            max_heartbeat_throttle_interval=timedelta(milliseconds=50),
            default_heartbeat_throttle_interval=timedelta(milliseconds=50),
            max_concurrent_activities=1,
        ),
    ):
        handle = await env.client.start_workflow(
            UploadWorkflow.run,
            UploadWorkflowInput(episode_id="ep-1", upload_task_queue=media_queue),
            id=f"episode-ep-1-upload-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
        if cancel:
            await asyncio.wait_for(mocks.started.wait(), timeout=10)
            await handle.cancel()
        return await asyncio.wait_for(handle.result(), timeout=60)


async def test_happy_path_admits_uploads_and_marks_uploaded(env) -> None:
    mocks = Mocks()
    result = await _run(env, mocks)
    assert mocks.calls == ["admit", "upload", "processing", "mark_uploaded"]
    assert result.status == EpisodeStatus.UPLOADED.value
    assert result.upload is not None and result.upload.video_id == "vid00000001"
    (req,) = mocks.processing_requests
    assert req.video_id == "vid00000001" and req.episode_id == "ep-1"
    assert result.processing is not None and result.processing.upload_status == "processed"


async def test_not_admitted_does_nothing(env) -> None:
    mocks = Mocks(admitted=False)
    result = await _run(env, mocks)
    assert mocks.calls == ["admit"] and not result.admitted and result.status == "uploaded"


async def test_transient_failure_is_retried(env) -> None:
    mocks = Mocks(errors=[_error("TransientError")])
    result = await _run(env, mocks)
    assert mocks.calls == ["admit", "upload", "upload", "processing", "mark_uploaded"]
    assert result.status == EpisodeStatus.UPLOADED.value


async def test_quota_retry_exhaustion_blocks(env) -> None:
    mocks = Mocks(always_fail=_error("UploadQuotaExceededError"))
    result = await _run(env, mocks)
    assert mocks.calls.count("upload") == UPLOAD_MAX_ATTEMPTS
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.RETRYABLE.value and failure.retry_exhausted
    assert failure.job_id == "job-1"
    assert result.status == "blocked"


@pytest.mark.parametrize(
    ("type_name", "expected"),
    [
        ("UploadOutcomeUnknownError", FailureClass.NEEDS_INPUT),
        ("UploadAuthError", FailureClass.NEEDS_INPUT),
        ("UploadsPausedError", FailureClass.NEEDS_INPUT),
        ("UploadRejectedError", FailureClass.NEEDS_INPUT),
        ("UploadIntegrityError", FailureClass.PERMANENT),
    ],
)
async def test_non_retryable_failures_are_recorded_once(env, type_name, expected) -> None:
    mocks = Mocks(always_fail=_error(type_name, non_retryable=True))
    await _run(env, mocks)
    assert mocks.calls == ["admit", "upload", "record_failure"]
    (failure,) = mocks.failures
    assert failure.failure_class == expected.value and not failure.retry_exhausted


async def test_cancel_stops_the_upload_then_records_and_reraises(env) -> None:
    mocks = Mocks(hang=True)
    with pytest.raises(WorkflowFailureError) as info:
        await _run(env, mocks, cancel=True)
    assert isinstance(info.value.cause, CancelledError)
    assert mocks.cancelled and mocks.calls[-2:] == ["upload_cancelled", "record_failure"]
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value


async def test_processing_pending_is_polled_until_processed(env) -> None:
    pending = _error("UploadProcessingPendingError")
    mocks = Mocks(processing_errors=[pending, pending])
    result = await _run(env, mocks)
    assert mocks.calls == ["admit", "upload", "processing", "processing", "processing"] + [
        "mark_uploaded"
    ]
    assert result.status == EpisodeStatus.UPLOADED.value


async def test_processing_failed_is_recorded_once_and_never_marks_uploaded(env) -> None:
    mocks = Mocks(processing_always_fail=_error("UploadProcessingFailedError", non_retryable=True))
    result = await _run(env, mocks)
    assert mocks.calls == ["admit", "upload", "processing", "record_failure"]
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value
    assert not failure.retry_exhausted and result.status == "blocked"


async def test_processing_that_never_finishes_blocks_after_the_deadline(env) -> None:
    mocks = Mocks(processing_always_fail=_error("UploadProcessingPendingError"))
    result = await _run(env, mocks)
    assert "mark_uploaded" not in mocks.calls and mocks.calls.count("upload") == 1
    assert mocks.calls.count("processing") > 3
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.RETRYABLE.value and failure.retry_exhausted
    assert result.status == "blocked"


def test_processing_retry_policy_matches_adr_0022() -> None:
    assert PROCESSING_RETRY_POLICY.initial_interval == timedelta(seconds=30)
    assert PROCESSING_RETRY_POLICY.backoff_coefficient == 2.0
    assert PROCESSING_RETRY_POLICY.maximum_interval == timedelta(minutes=10)
    assert PROCESSING_RETRY_POLICY.maximum_attempts == 0
    assert "UploadProcessingFailedError" in (
        PROCESSING_RETRY_POLICY.non_retryable_error_types or []
    )
    assert "UploadProcessingPendingError" not in (
        PROCESSING_RETRY_POLICY.non_retryable_error_types or []
    )
    assert timedelta(hours=6) == PROCESSING_SCHEDULE_TO_CLOSE


def test_start_to_close_uses_the_admit_estimate_plus_lookup_margin() -> None:
    assert UploadWorkflowInput(episode_id="e").upload_task_queue == UPLOAD_MEDIA_TASK_QUEUE
    assert upload_start_to_close(0) == timedelta(
        seconds=DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS + UPLOAD_ACTIVITY_MARGIN_SECONDS
    )
    assert upload_start_to_close(5000) == timedelta(seconds=5000 + UPLOAD_ACTIVITY_MARGIN_SECONDS)
