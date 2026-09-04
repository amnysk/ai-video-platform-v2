"""ScriptWorkflow。

**工程の順序とラウンド数を知る唯一の場所**（INV-4 / INV-5）。
ここはI/Oをせず、すべての副作用をActivityへ委ねる。

Phase 1 の骨組みと違い、**有料Activityは Temporal の自動retryに委ねない**
（ADR-0013）。`maximum_attempts=1` とし、retry は workflow のラウンドとして
予約台帳を通す。自動retryに任せると課金呼び出しが台帳を経ずに増える。
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
    from workers.planning.activities import (
        CreateJobRequest,
        EpisodeRef,
        GenerateScriptRequest,
        RecordFailureRequest,
        ScriptActivities,
    )

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
#: Codex は数分かかりうる。adapter 側の subprocess timeout より**長く**取る
#: （短いと Temporal が書き込み途中で Activity を切る）。
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


@dataclass
class ScriptWorkflowInput:
    episode_id: str
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass
class ScriptWorkflowResult:
    episode_id: str
    #: **application domain state**。Temporalのworkflow実行状態ではない（INV-8）。
    status: str
    artifact_object_key: str | None = None
    sha256: str | None = None
    rounds_used: int = 0
    reused_existing_artifact: bool = False


@workflow.defn(name="ScriptWorkflow")
class ScriptWorkflow:
    @workflow.run
    async def run(self, request: ScriptWorkflowInput) -> ScriptWorkflowResult:
        episode_ref = EpisodeRef(episode_id=request.episode_id)

        await workflow.execute_activity_method(
            ScriptActivities.mark_episode_in_progress,
            episode_ref,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        job_id: str = await workflow.execute_activity_method(
            ScriptActivities.create_script_job,
            CreateJobRequest(episode_id=request.episode_id, max_attempts=request.max_attempts),
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        last_error: ActivityError | None = None
        for round_number in range(1, request.max_attempts + 1):
            try:
                result = await workflow.execute_activity_method(
                    ScriptActivities.generate_script,
                    GenerateScriptRequest(
                        episode_id=request.episode_id, job_id=job_id, round=round_number
                    ),
                    start_to_close_timeout=GENERATE_ACTIVITY_TIMEOUT,
                    retry_policy=GENERATE_RETRY_POLICY,
                )
            except ActivityError as err:
                last_error = err
                if not self._is_worth_another_round(err):
                    break
                continue

            status: str = await workflow.execute_activity_method(
                ScriptActivities.mark_script_ready,
                episode_ref,
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                retry_policy=STATE_RETRY_POLICY,
            )
            return ScriptWorkflowResult(
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
        """失敗クラスは**例外の型名**から引く（failure-policy §1）。

        未知の型名は ``needs_input`` になり、Episodeは blocked で生き残る（INV-12）。
        """
        cause = err.cause
        type_name = cause.type if isinstance(cause, ApplicationError) else None
        return failure_class_from_type_name(type_name)

    def _is_worth_another_round(self, err: ActivityError) -> bool:
        """もう1ラウンド回す価値があるか。

        ``needs_input`` / ``permanent`` は同じ入力で繰り返しても変わらないので、
        課金呼び出しを増やさずに打ち切る。
        """
        return self._failure_class(err) in {FailureClass.TRANSIENT, FailureClass.RETRYABLE}

    async def _settle_failure(
        self, request: ScriptWorkflowInput, job_id: str, err: ActivityError
    ) -> ScriptWorkflowResult:
        failure_class = self._failure_class(err)
        cause = err.cause
        summary = str(cause) if cause is not None else str(err)

        outcome = await workflow.execute_activity_method(
            ScriptActivities.record_failure,
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
        return ScriptWorkflowResult(episode_id=request.episode_id, status=outcome.episode_status)
