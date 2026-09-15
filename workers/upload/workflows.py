"""UploadWorkflow（ADR-0020）。

**工程の順序を知る唯一の場所**（INV-4 / INV-5）。I/O をしない。構造は RenderWorkflow と同じ::

    admit → upload_final_video（queue upload-media、並行数 1、retry 上限つき）
      → await_processing（YouTube の処理完了を RetryPolicy の間隔で待つ。ADR-0022）→ mark_uploaded
      処理の拒否・失敗 / 期限切れ → record_failure（blocked）
      再開は予約の spent で再投稿せず、照会からやり直す
      投稿が失敗 → 失敗クラスで record_failure（retryable を使い切ったら blocked）
      workflow の cancel → 投稿 Activity の cancel 完了を待ち、record_failure → cancel を再送出
                           （予約の session は残るので、POST で続きから再開できる）

Activity は**名前**で呼ぶ（``contracts.upload_activities``）。実装を import しない（INV-3）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from contracts.states import (
        RETRYABLE_FAILURE_CLASSES,
        UPLOAD_MEDIA_TASK_QUEUE,
        UPLOAD_WORKFLOW,
        FailureClass,
    )
    from contracts.upload import (
        DEFAULT_UPLOAD_HEARTBEAT_TIMEOUT_SECONDS,
        DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS,
        DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS,
        DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS,
        DEFAULT_UPLOAD_PROCESSING_DEADLINE_SECONDS,
        DEFAULT_UPLOAD_PROCESSING_INITIAL_INTERVAL_SECONDS,
        DEFAULT_UPLOAD_PROCESSING_MAX_INTERVAL_SECONDS,
    )
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
    from domain.errors import NON_RETRYABLE_ERROR_TYPE_NAMES, failure_class_from_type_name

WORKFLOW_NAME, TASK_QUEUE = UPLOAD_WORKFLOW

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
STATE_SCHEDULE_TO_CLOSE = timedelta(hours=1)
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

#: start_to_close の余裕: マーカー照合の待ち（回数 × 間隔）+ 受領の保存など 5 分。
UPLOAD_ACTIVITY_MARGIN_SECONDS = (
    DEFAULT_UPLOAD_MARKER_LOOKUP_ATTEMPTS * DEFAULT_UPLOAD_MARKER_LOOKUP_DELAY_SECONDS + 5 * 60
)

#: 投稿は retryable の型だけ上限つきで retry する。quota は長めに待つ（ADR-0020 §11）。
UPLOAD_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=10),
    maximum_attempts=UPLOAD_MAX_ATTEMPTS,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)


#: 処理状態の照会（ADR-0022）。1回は短い読み取り。待ちは retry の間隔で表し、期限で blocked にする
PROCESSING_START_TO_CLOSE = timedelta(minutes=2)
PROCESSING_SCHEDULE_TO_CLOSE = timedelta(seconds=DEFAULT_UPLOAD_PROCESSING_DEADLINE_SECONDS)
PROCESSING_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=DEFAULT_UPLOAD_PROCESSING_INITIAL_INTERVAL_SECONDS),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=DEFAULT_UPLOAD_PROCESSING_MAX_INTERVAL_SECONDS),
    maximum_attempts=0,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)


@dataclass
class UploadWorkflowInput:
    episode_id: str
    #: 投稿 Activity の task queue。テストが本物の worker と取り合わないよう差し替える
    upload_task_queue: str = UPLOAD_MEDIA_TASK_QUEUE


@dataclass
class UploadWorkflowResult:
    episode_id: str
    #: **application domain state**（INV-8）
    status: str
    admitted: bool = True
    owned: bool = True
    upload: UploadFinalVideoResult | None = None
    failure_class: str | None = None
    processing: UploadAwaitProcessingResult | None = None


@dataclass
class _StageFailure:
    failure_class: FailureClass
    summary: str
    retry_exhausted: bool
    job_id: str = ""


def upload_start_to_close(upload_timeout_seconds: int) -> timedelta:
    base = upload_timeout_seconds or DEFAULT_UPLOAD_MIN_TIMEOUT_SECONDS
    return timedelta(seconds=max(1, base) + UPLOAD_ACTIVITY_MARGIN_SECONDS)


def failed_job_id(err: ActivityError) -> str:
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.details:
        first = cause.details[0]
        if isinstance(first, str):
            return first
    return ""


def classify_upload_failure(err: ActivityError) -> FailureClass:
    """型名から失敗クラスを引く。Temporal の timeout は retryable、未知は needs_input（INV-12）。"""
    cause = err.cause
    if isinstance(cause, TemporalTimeoutError):
        return FailureClass.RETRYABLE
    type_name = cause.type if isinstance(cause, ApplicationError) else None
    return failure_class_from_type_name(type_name)


def _summary(err: ActivityError) -> str:
    cause = err.cause
    return str(cause if cause is not None else err)[:1000]


@workflow.defn(name=WORKFLOW_NAME)
class UploadWorkflow:
    @workflow.run
    async def run(self, request: UploadWorkflowInput) -> UploadWorkflowResult:
        info = workflow.info()
        admit: UploadAdmitResult = await workflow.execute_activity(
            UPLOAD_ADMIT,
            UploadAdmitRequest(
                episode_id=request.episode_id, workflow_id=info.workflow_id, run_id=info.run_id
            ),
            result_type=UploadAdmitResult,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        if not admit.admitted:
            return UploadWorkflowResult(
                episode_id=request.episode_id, status=admit.status, admitted=False
            )
        try:
            return await self._admitted(request, admit.upload_timeout_seconds)
        except asyncio.CancelledError:
            reason = workflow.cancellation_reason()
            await self._settle_uncancellable(
                request,
                _StageFailure(
                    FailureClass.NEEDS_INPUT,
                    f"upload workflow cancelled{f': {reason}' if reason else ''}",
                    retry_exhausted=False,
                ),
            )
            raise

    async def _settle_uncancellable(
        self, request: UploadWorkflowInput, failure: _StageFailure
    ) -> None:
        settle = asyncio.ensure_future(self._settle(request, failure))
        while True:
            try:
                await asyncio.shield(settle)
                return
            except asyncio.CancelledError:
                if settle.done():
                    raise

    async def _admitted(
        self, request: UploadWorkflowInput, upload_timeout_seconds: int
    ) -> UploadWorkflowResult:
        info = workflow.info()
        try:
            uploaded: UploadFinalVideoResult = await workflow.execute_activity(
                UPLOAD_FINAL_VIDEO,
                UploadFinalVideoRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                ),
                result_type=UploadFinalVideoResult,
                task_queue=request.upload_task_queue,
                start_to_close_timeout=upload_start_to_close(upload_timeout_seconds),
                heartbeat_timeout=timedelta(seconds=DEFAULT_UPLOAD_HEARTBEAT_TIMEOUT_SECONDS),
                retry_policy=UPLOAD_RETRY_POLICY,
                # 送信を止めてから record_failure する（まだ走っている投稿が台帳を書かないように）
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except ActivityError as err:
            if isinstance(err.cause, CancelledError):
                raise asyncio.CancelledError() from err
            cls = classify_upload_failure(err)
            return await self._settle(
                request,
                _StageFailure(
                    cls,
                    _summary(err),
                    retry_exhausted=cls in RETRYABLE_FAILURE_CLASSES,
                    job_id=failed_job_id(err),
                ),
            )

        try:
            processed: UploadAwaitProcessingResult = await workflow.execute_activity(
                UPLOAD_AWAIT_PROCESSING,
                UploadAwaitProcessingRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    video_id=uploaded.video_id,
                ),
                result_type=UploadAwaitProcessingResult,
                start_to_close_timeout=PROCESSING_START_TO_CLOSE,
                schedule_to_close_timeout=PROCESSING_SCHEDULE_TO_CLOSE,
                retry_policy=PROCESSING_RETRY_POLICY,
            )
        except ActivityError as err:
            if isinstance(err.cause, CancelledError):
                raise asyncio.CancelledError() from err
            cls = classify_upload_failure(err)
            return await self._settle(
                request,
                _StageFailure(cls, _summary(err), retry_exhausted=cls in RETRYABLE_FAILURE_CLASSES),
            )

        marked: UploadMarkUploadedResult = await workflow.execute_activity(
            UPLOAD_MARK_UPLOADED,
            UploadMarkUploadedRequest(
                episode_id=request.episode_id, workflow_id=info.workflow_id, run_id=info.run_id
            ),
            result_type=UploadMarkUploadedResult,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        return UploadWorkflowResult(
            episode_id=request.episode_id,
            status=marked.status,
            owned=marked.owned,
            upload=uploaded,
            processing=processed,
        )

    async def _settle(
        self, request: UploadWorkflowInput, failure: _StageFailure
    ) -> UploadWorkflowResult:
        info = workflow.info()
        outcome: UploadFailureOutcome = await workflow.execute_activity(
            UPLOAD_RECORD_FAILURE,
            UploadRecordFailureRequest(
                episode_id=request.episode_id,
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                failure_class=failure.failure_class.value,
                error_summary=failure.summary,
                retry_exhausted=failure.retry_exhausted,
                job_id=failure.job_id,
            ),
            result_type=UploadFailureOutcome,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        return UploadWorkflowResult(
            episode_id=request.episode_id,
            status=outcome.episode_status,
            owned=outcome.owned,
            failure_class=failure.failure_class.value,
        )


__all__ = [
    "PROCESSING_RETRY_POLICY",
    "PROCESSING_SCHEDULE_TO_CLOSE",
    "PROCESSING_START_TO_CLOSE",
    "STATE_RETRY_POLICY",
    "TASK_QUEUE",
    "UPLOAD_ACTIVITY_MARGIN_SECONDS",
    "UPLOAD_RETRY_POLICY",
    "WORKFLOW_NAME",
    "UploadWorkflow",
    "UploadWorkflowInput",
    "UploadWorkflowResult",
    "classify_upload_failure",
    "failed_job_id",
    "upload_start_to_close",
]
