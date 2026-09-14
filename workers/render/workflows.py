"""RenderWorkflow（ADR-0019）。

**工程の順序を知る唯一の場所**（INV-4 / INV-5）。I/O をしない。

構造::

    admit → render_final_video（queue render-media の重い Activity。retry 上限つき）→ mark_ready
      描画が失敗 → 失敗クラスで record_failure（retryable を使い切ったら blocked）
      workflow の cancel → 描画 Activity の cancel 完了を待ち、cancel されない形で
                           record_failure → cancel を再送出

Activity は**名前**で呼ぶ（``contracts.render_activities``）。実装を import しない（INV-3）。
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
    from contracts.render import (
        DEFAULT_RENDER_HEARTBEAT_TIMEOUT_SECONDS,
        DEFAULT_RENDER_PROFILE_ID,
        DEFAULT_RENDER_TIMEOUT_SECONDS,
        RENDER_PROFILES,
    )
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
    from contracts.states import (
        RENDER_MEDIA_TASK_QUEUE,
        RENDER_WORKFLOW,
        RETRYABLE_FAILURE_CLASSES,
        FailureClass,
    )
    from domain.errors import NON_RETRYABLE_ERROR_TYPE_NAMES, failure_class_from_type_name

WORKFLOW_NAME, TASK_QUEUE = RENDER_WORKFLOW

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
#: 状態系 Activity は Episode を ``in_progress`` から出す唯一の経路なので DB の一時障害で諦めない
#: （production と同じ方針 / ADR-0017）。retry しないのは needs_input / permanent の型だけ。
STATE_SCHEDULE_TO_CLOSE = timedelta(hours=1)
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

#: 描画 Activity の start_to_close = エンジンの timeout + 描画以外の I/O の余裕（ADR-0019 §8）:
#:   margin = RENDER_ACTIVITY_MARGIN_SECONDS
#:          + RENDER_IO_SECONDS_PER_OUTPUT_SECOND × ceil(profile.limits.max_duration_ms / 1000)
#: 描画以外の I/O（素材の取り出し・検査、完成動画の probe・sha256・保存・読み戻し）は尺に比例する。
#: エンジン自身の timeout（``RenderEngineTimeoutError``）が先に効くようにする。
RENDER_ACTIVITY_MARGIN_SECONDS = 5 * 60
RENDER_IO_SECONDS_PER_OUTPUT_SECOND = 2

#: 描画は retryable の型だけ上限つきで retry する（ADR-0019 §8）。
RENDER_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=RENDER_MAX_ATTEMPTS,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)


@dataclass
class RenderWorkflowInput:
    episode_id: str
    render_profile_id: str = DEFAULT_RENDER_PROFILE_ID
    #: 描画 Activity の task queue。テストが共有サーバ上で本物の worker と取り合わないよう差し替える
    render_task_queue: str = RENDER_MEDIA_TASK_QUEUE


@dataclass
class RenderWorkflowResult:
    episode_id: str
    #: **application domain state**（INV-8）
    status: str
    admitted: bool = True
    owned: bool = True
    final_video: RenderFinalVideoResult | None = None
    failure_class: str | None = None


@dataclass
class _StageFailure:
    failure_class: FailureClass
    summary: str
    retry_exhausted: bool
    job_id: str = ""


def render_start_to_close(render_timeout_seconds: int, render_profile_id: str) -> timedelta:
    """式は ``RENDER_ACTIVITY_MARGIN_SECONDS`` の注記。未知の profile は最短の余裕。"""
    profile = RENDER_PROFILES.get(render_profile_id)
    output_seconds = -(-profile.limits.max_duration_ms // 1000) if profile is not None else 0
    margin = RENDER_ACTIVITY_MARGIN_SECONDS + RENDER_IO_SECONDS_PER_OUTPUT_SECOND * output_seconds
    return timedelta(seconds=max(1, render_timeout_seconds) + margin)


def failed_job_id(err: ActivityError) -> str:
    """描画 Activity が ApplicationError の details に載せた job id（無ければ空文字）。"""
    cause = err.cause
    if isinstance(cause, ApplicationError) and cause.details:
        first = cause.details[0]
        if isinstance(first, str):
            return first
    return ""


def classify_render_failure(err: ActivityError) -> FailureClass:
    """失敗クラスは**例外の型名**から引く。

    Temporal の timeout（heartbeat / start_to_close）は retry policy で再実行された末の失敗なので
    ``retryable``（使い切り → blocked）。それ以外の未知の型は ``needs_input``（INV-12）。
    """
    cause = err.cause
    if isinstance(cause, TemporalTimeoutError):
        return FailureClass.RETRYABLE
    type_name = cause.type if isinstance(cause, ApplicationError) else None
    return failure_class_from_type_name(type_name)


def _summary(err: ActivityError) -> str:
    cause = err.cause
    return str(cause if cause is not None else err)[:1000]


@workflow.defn(name=WORKFLOW_NAME)
class RenderWorkflow:
    @workflow.run
    async def run(self, request: RenderWorkflowInput) -> RenderWorkflowResult:
        info = workflow.info()
        admit: RenderAdmitResult = await workflow.execute_activity(
            RENDER_ADMIT,
            RenderAdmitRequest(
                episode_id=request.episode_id, workflow_id=info.workflow_id, run_id=info.run_id
            ),
            result_type=RenderAdmitResult,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        if not admit.admitted:
            return RenderWorkflowResult(
                episode_id=request.episode_id, status=admit.status, admitted=False
            )
        timeout = admit.render_timeout_seconds or DEFAULT_RENDER_TIMEOUT_SECONDS
        try:
            return await self._admitted(request, timeout)
        except asyncio.CancelledError:
            # 入場後の cancel。Episode を in_progress に置き去りにしない。
            # 人間が止めたので needs_input → blocked（POST で再開 / production と同じ）。
            reason = workflow.cancellation_reason()
            await self._settle_uncancellable(
                request,
                _StageFailure(
                    FailureClass.NEEDS_INPUT,
                    f"render workflow cancelled{f': {reason}' if reason else ''}",
                    retry_exhausted=False,
                ),
            )
            raise

    async def _settle_uncancellable(
        self, request: RenderWorkflowInput, failure: _StageFailure
    ) -> None:
        """cancel を受けた後の記録。重ねて cancel されても記録の完了を待ってから抜ける。"""
        settle = asyncio.ensure_future(self._settle(request, failure))
        while True:
            try:
                await asyncio.shield(settle)
                return
            except asyncio.CancelledError:
                if settle.done():
                    raise

    async def _admitted(
        self, request: RenderWorkflowInput, render_timeout_seconds: int
    ) -> RenderWorkflowResult:
        info = workflow.info()
        try:
            rendered: RenderFinalVideoResult = await workflow.execute_activity(
                RENDER_FINAL_VIDEO,
                RenderFinalVideoRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    render_profile_id=request.render_profile_id,
                ),
                result_type=RenderFinalVideoResult,
                task_queue=request.render_task_queue,
                start_to_close_timeout=render_start_to_close(
                    render_timeout_seconds, request.render_profile_id
                ),
                heartbeat_timeout=timedelta(seconds=DEFAULT_RENDER_HEARTBEAT_TIMEOUT_SECONDS),
                retry_policy=RENDER_RETRY_POLICY,
                # cancel は heartbeat で Activity に届き、エンジンの子プロセスを止めて作業領域を
                # 片付けてから終わる。それを待たずに record_failure すると、まだ走っている描画が
                # job / Artifact を書く。
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
        except ActivityError as err:
            if isinstance(err.cause, CancelledError):
                raise asyncio.CancelledError() from err
            cls = classify_render_failure(err)
            return await self._settle(
                request,
                _StageFailure(
                    cls,
                    _summary(err),
                    retry_exhausted=cls in RETRYABLE_FAILURE_CLASSES,
                    job_id=failed_job_id(err),
                ),
            )

        ready: RenderMarkReadyResult = await workflow.execute_activity(
            RENDER_MARK_READY,
            RenderMarkReadyRequest(
                episode_id=request.episode_id, workflow_id=info.workflow_id, run_id=info.run_id
            ),
            result_type=RenderMarkReadyResult,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        return RenderWorkflowResult(
            episode_id=request.episode_id,
            status=ready.status,
            owned=ready.owned,
            final_video=rendered,
        )

    async def _settle(
        self, request: RenderWorkflowInput, failure: _StageFailure
    ) -> RenderWorkflowResult:
        info = workflow.info()
        outcome: RenderFailureOutcome = await workflow.execute_activity(
            RENDER_RECORD_FAILURE,
            RenderRecordFailureRequest(
                episode_id=request.episode_id,
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                failure_class=failure.failure_class.value,
                error_summary=failure.summary,
                retry_exhausted=failure.retry_exhausted,
                job_id=failure.job_id,
            ),
            result_type=RenderFailureOutcome,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        return RenderWorkflowResult(
            episode_id=request.episode_id,
            status=outcome.episode_status,
            owned=outcome.owned,
            failure_class=failure.failure_class.value,
        )


__all__ = [
    "RENDER_ACTIVITY_MARGIN_SECONDS",
    "RENDER_RETRY_POLICY",
    "STATE_RETRY_POLICY",
    "TASK_QUEUE",
    "WORKFLOW_NAME",
    "RenderWorkflow",
    "RenderWorkflowInput",
    "RenderWorkflowResult",
    "classify_render_failure",
    "failed_job_id",
    "RENDER_IO_SECONDS_PER_OUTPUT_SECOND",
    "render_start_to_close",
]
