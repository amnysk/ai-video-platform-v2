"""EpisodeSkeletonWorkflow。

**工程の順序を知る唯一の場所**（INV-4 / INV-5）。ここはI/Oをせず、
すべての副作用をActivityへ委ねる。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from contracts.states import DEFAULT_MAX_ATTEMPTS, FailureClass
    from domain.errors import (
        NON_RETRYABLE_ERROR_TYPE_NAMES,
        failure_class_from_type_name,
    )
    from workers.dummy.activities import (
        CreateJobRequest,
        DummyActivities,
        EpisodeRef,
        ProduceArtifactRequest,
        RecordFailureRequest,
    )

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
WORK_ACTIVITY_TIMEOUT = timedelta(minutes=5)

#: 状態を書くだけのActivityは短く自動retryする（transient扱い）。
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=50),
    maximum_interval=timedelta(seconds=1),
    maximum_attempts=5,
)


@dataclass
class EpisodeWorkflowInput:
    episode_id: str
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass
class EpisodeWorkflowResult:
    episode_id: str
    #: **application domain state**。Temporalのworkflow実行状態ではない（INV-8）。
    status: str
    artifact_object_key: str | None = None
    sha256: str | None = None


@workflow.defn(name="EpisodeSkeletonWorkflow")
class EpisodeSkeletonWorkflow:
    @workflow.run
    async def run(self, request: EpisodeWorkflowInput) -> EpisodeWorkflowResult:
        episode_ref = EpisodeRef(episode_id=request.episode_id)

        await workflow.execute_activity_method(
            DummyActivities.mark_episode_in_progress,
            episode_ref,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        job_id: str = await workflow.execute_activity_method(
            DummyActivities.create_dummy_job,
            CreateJobRequest(episode_id=request.episode_id, max_attempts=request.max_attempts),
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        try:
            artifact = await workflow.execute_activity_method(
                DummyActivities.produce_dummy_artifact,
                ProduceArtifactRequest(episode_id=request.episode_id, job_id=job_id),
                start_to_close_timeout=WORK_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(
                    initial_interval=timedelta(milliseconds=50),
                    maximum_interval=timedelta(seconds=2),
                    maximum_attempts=request.max_attempts,
                    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
                ),
            )
        except ActivityError as err:
            return await self._settle_failure(request, job_id, err)

        status: str = await workflow.execute_activity_method(
            DummyActivities.complete_episode,
            episode_ref,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )
        return EpisodeWorkflowResult(
            episode_id=request.episode_id,
            status=status,
            artifact_object_key=artifact.object_key,
            sha256=artifact.sha256,
        )

    async def _settle_failure(
        self, request: EpisodeWorkflowInput, job_id: str, err: ActivityError
    ) -> EpisodeWorkflowResult:
        """失敗を確定する。

        失敗クラスは**例外の型名**から引く（failure-policy §1）。
        未知の型名は ``needs_input`` になり、Episodeは blocked で生き残る（INV-12）。
        """
        cause = err.cause
        type_name = cause.type if isinstance(cause, ApplicationError) else None
        failure_class = failure_class_from_type_name(type_name)
        summary = str(cause) if cause is not None else str(err)

        outcome = await workflow.execute_activity_method(
            DummyActivities.record_failure,
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
        return EpisodeWorkflowResult(episode_id=request.episode_id, status=outcome.episode_status)
