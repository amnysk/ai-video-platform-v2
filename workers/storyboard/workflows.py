"""StoryboardWorkflow（ADR-0015）。

**工程の順序とラウンド数を知る唯一の場所**（INV-4 / INV-5）。
ここはI/Oをせず、すべての副作用をActivityへ委ねる。

有料Activityは Temporal の自動retryに委ねない（ADR-0013）。`maximum_attempts=1` とし、
retry は workflow のラウンドとして予約台帳を通す。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from contracts.states import DEFAULT_MAX_ATTEMPTS, STORYBOARD_WORKFLOW, FailureClass
    from domain.errors import (
        NON_RETRYABLE_ERROR_TYPE_NAMES,
        failure_class_from_type_name,
    )
    from workers.storyboard.activities import (
        CreateJobRequest,
        EpisodeRef,
        GenerateStoryboardRequest,
        RecordFailureRequest,
        StoryboardActivities,
    )

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
#: 生成は数分かかりうる。adapter 側の subprocess timeout より**長く**取る。
GENERATE_ACTIVITY_TIMEOUT = timedelta(minutes=20)

STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=50),
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=5,
)

#: 有料Activityは自動retryしない（ADR-0013）。retryはworkflowのラウンド。
GENERATE_RETRY_POLICY = RetryPolicy(
    maximum_attempts=1,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

WORKFLOW_NAME, TASK_QUEUE = STORYBOARD_WORKFLOW


@dataclass
class StoryboardWorkflowInput:
    episode_id: str
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass
class StoryboardWorkflowResult:
    episode_id: str
    #: **application domain state**。Temporalのworkflow実行状態ではない（INV-8）。
    status: str
    artifact_object_key: str | None = None
    sha256: str | None = None
    rounds_used: int = 0
    reused_existing_artifact: bool = False
    #: 駐機点に居なかったため工程に入らなかった（何も書いていない）。
    admitted: bool = True


@workflow.defn(name=WORKFLOW_NAME)
class StoryboardWorkflow:
    @workflow.run
    async def run(self, request: StoryboardWorkflowInput) -> StoryboardWorkflowResult:
        episode_ref = EpisodeRef(episode_id=request.episode_id)

        admit = await workflow.execute_activity_method(
            StoryboardActivities.admit_episode,
            episode_ref,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )
        if not admit.admitted:
            return StoryboardWorkflowResult(
                episode_id=request.episode_id, status=admit.status, admitted=False
            )

        job_id: str = await workflow.execute_activity_method(
            StoryboardActivities.create_job,
            CreateJobRequest(episode_id=request.episode_id, max_attempts=request.max_attempts),
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        last_error: ActivityError | None = None
        for round_number in range(1, request.max_attempts + 1):
            try:
                result = await workflow.execute_activity_method(
                    StoryboardActivities.generate_storyboard,
                    GenerateStoryboardRequest(
                        episode_id=request.episode_id, job_id=job_id, round=round_number
                    ),
                    start_to_close_timeout=GENERATE_ACTIVITY_TIMEOUT,
                    retry_policy=GENERATE_RETRY_POLICY,
                )
            except ActivityError as err:
                last_error = err
                if self._failure_class(err) not in {FailureClass.TRANSIENT, FailureClass.RETRYABLE}:
                    break  # needs_input / permanent は課金を増やさず打ち切る
                continue

            status: str = await workflow.execute_activity_method(
                StoryboardActivities.mark_ready,
                episode_ref,
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                retry_policy=STATE_RETRY_POLICY,
            )
            return StoryboardWorkflowResult(
                episode_id=request.episode_id,
                status=status,
                artifact_object_key=result.object_key,
                sha256=result.sha256,
                rounds_used=round_number,
                reused_existing_artifact=result.reused,
            )

        assert last_error is not None
        return await self._settle_failure(request, job_id, last_error)

    @staticmethod
    def _failure_class(err: ActivityError) -> FailureClass:
        """失敗クラスは**例外の型名**から引く。未知の型名は ``needs_input``（INV-12）。"""
        cause = err.cause
        type_name = cause.type if isinstance(cause, ApplicationError) else None
        return failure_class_from_type_name(type_name)

    async def _settle_failure(
        self, request: StoryboardWorkflowInput, job_id: str, err: ActivityError
    ) -> StoryboardWorkflowResult:
        failure_class = self._failure_class(err)
        cause = err.cause
        summary = str(cause) if cause is not None else str(err)

        outcome = await workflow.execute_activity_method(
            StoryboardActivities.record_failure,
            RecordFailureRequest(
                episode_id=request.episode_id,
                job_id=job_id,
                failure_class=failure_class.value,
                error_summary=summary,
                retry_exhausted=failure_class in {FailureClass.TRANSIENT, FailureClass.RETRYABLE},
            ),
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )
        return StoryboardWorkflowResult(
            episode_id=request.episode_id, status=outcome.episode_status
        )
