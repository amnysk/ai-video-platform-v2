"""ResearchWorkflow（ADR-0037 §8.5）。

**Research の実行の順序と retry の上限を知る唯一の場所**（INV-4 / INV-5）。I/O・壁時計・乱数・DB を
使わない（決定性。``tests/architecture/test_research_workflow_determinism.py``）::

    research_execute（依頼全体を 1 回実行する。一時障害は上限つきで retry）
      → 結果（参照と件数）を返す
    retry を使い切った・retry しない失敗
      → research_record_failure（DB に blocked / failed を記録するのは Activity）→ その結果を返す

- Activity は**名前**で呼ぶ（``contracts.research``）。実装を import しない（INV-3）
- 履歴に載るのは依頼 ID・状態・理由コード・成果物の参照（id・sha256）・件数だけ（ADR-0029 の形）
- non-retryable は基底の表 ∪ research の表（``RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES``）。
  research の permanent / needs_input の例外を Temporal が retry しない
- retry は executor が新しい番号の予約を取る（成功済みの呼び出しは生データから読み、送り直さない。
  成否不明の呼び出しは送り直さず ``blocked``）。retry も合計で呼び出しの枠を数える（INV-36）
- 依頼の期限（``deadline_seconds``）は executor が ``started_at`` から測る。ここは Activity の
  最長時間（天井 + 余裕）と heartbeat だけを持つ
- Episode の工程はこの workflow を起動しない・待たない（INV-37。
  ``tests/architecture/test_daily_does_not_wait_for_research.py``）
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError

with workflow.unsafe.imports_passed_through():
    from contracts.research import (
        LIMIT_CEILING_DEADLINE_SECONDS,
        RESEARCH_EXECUTE_ACTIVITY,
        RESEARCH_EXECUTE_MAX_ATTEMPTS,
        RESEARCH_RECORD_FAILURE_ACTIVITY,
        RESEARCH_WORKFLOW_NAME,
        ResearchExecuteRequest,
        ResearchRecordFailureRequest,
        ResearchWorkflowInput,
        ResearchWorkflowOutput,
    )
    from domain.errors import TransientError
    from domain.research.errors import RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES

#: 依頼 1 件の実行の最長時間。依頼の期限の天井に、最後の呼び出しと成果物の確定の余裕を足す
#: （期限そのものは executor が守る。ここは worker が固まったときの最後の砦）
EXECUTE_START_TO_CLOSE = timedelta(seconds=LIMIT_CEILING_DEADLINE_SECONDS) + timedelta(minutes=30)
#: worker が落ちたことを知る時間（Activity は ``RESEARCH_HEARTBEAT_INTERVAL_SECONDS`` ごとに送る）
EXECUTE_HEARTBEAT_TIMEOUT = timedelta(minutes=2)
#: DB だけの短い Activity
STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)

_NON_RETRYABLE = list(RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES)

#: 依頼の実行。**retryable だけ・上限つき**（指数バックオフ）
EXECUTE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=RESEARCH_EXECUTE_MAX_ATTEMPTS,
    non_retryable_error_types=_NON_RETRYABLE,
)

#: 失敗の記録（DB だけ）
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=5,
    non_retryable_error_types=_NON_RETRYABLE,
)

_SUMMARY_MAX = 500


def _failure_type(err: ActivityError) -> str | None:
    cause = err.cause
    if isinstance(cause, ApplicationError):
        return cause.type
    if isinstance(cause, TemporalTimeoutError):
        return TransientError.__name__  # Activity の timeout（heartbeat を含む）は一時障害
    return type(cause).__name__ if cause is not None else None


def _summary(err: ActivityError) -> str:
    cause = err.cause
    text = str(cause if cause is not None else err)
    return text[:_SUMMARY_MAX]


@workflow.defn(name=RESEARCH_WORKFLOW_NAME)
class ResearchWorkflow:
    @workflow.run
    async def run(self, request: ResearchWorkflowInput) -> ResearchWorkflowOutput:
        try:
            return await workflow.execute_activity(
                RESEARCH_EXECUTE_ACTIVITY,
                ResearchExecuteRequest(request_id=request.request_id),
                result_type=ResearchWorkflowOutput,
                start_to_close_timeout=EXECUTE_START_TO_CLOSE,
                heartbeat_timeout=EXECUTE_HEARTBEAT_TIMEOUT,
                retry_policy=EXECUTE_RETRY_POLICY,
            )
        except ActivityError as err:
            if isinstance(err.cause, CancelledError):
                raise asyncio.CancelledError() from err
            return await workflow.execute_activity(
                RESEARCH_RECORD_FAILURE_ACTIVITY,
                ResearchRecordFailureRequest(
                    request_id=request.request_id,
                    error_type=_failure_type(err),
                    summary=_summary(err),
                ),
                result_type=ResearchWorkflowOutput,
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                retry_policy=STATE_RETRY_POLICY,
            )
