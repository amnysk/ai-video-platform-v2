"""TopicPlannerWorkflow（ADR-0025）。

**Planner の手順と round 数を知る唯一の場所**（INV-4 / INV-5）。I/O をしない::

    topic_find_plan → あれば返す（reused。再生成しない / INV-22）
    topic_gather_context（Analytics の fallback と Content Memory）
    round 1..policy.max_rounds:
        topic_generate_candidates（LLM。maximum_attempts=1。契約違反は次の round / INV-23）
        topic_select_and_save（決定論の選択 + 1 transaction の保存）
            → plan を返す / 全候補が重複なら落ちた subject を避けて次の round（INV-24）
    使い切った → TopicPlanningExhaustedError（non-retryable）。Episode は作られない（INV-21）
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from contracts.states import FailureClass
    from contracts.topic_planning import (
        CONTENT_PROFILES,
        DEFAULT_PLANNER_POLICY,
        STRATEGY_PROFILES,
        FindPlanRequest,
        GenerateCandidatesRequest,
        SelectAndSaveRequest,
        TopicPlannerInput,
        TopicPlannerResult,
    )
    from domain.errors import (
        PermanentError,
        TopicPlanningExhaustedError,
        failure_class_from_type_name,
    )
    from workers.planning.topic_activities import TopicPlannerActivities
    from workers.planning.workflows import (
        GENERATE_ACTIVITY_TIMEOUT,
        GENERATE_RETRY_POLICY,
        STATE_ACTIVITY_TIMEOUT,
        STATE_RETRY_POLICY,
    )

#: live Analytics の取得（数本の HTTP）を含む
CONTEXT_ACTIVITY_TIMEOUT = timedelta(minutes=5)


@workflow.defn(name="TopicPlannerWorkflow")
class TopicPlannerWorkflow:
    @workflow.run
    async def run(self, request: TopicPlannerInput) -> TopicPlannerResult:
        if (
            request.strategy_profile_id not in STRATEGY_PROFILES
            or request.content_profile_id not in CONTENT_PROFILES
        ):
            # 設定誤りは再試行しても直らない
            raise ApplicationError(
                f"unknown profile: strategy={request.strategy_profile_id!r} "
                f"content={request.content_profile_id!r}",
                type=PermanentError.__name__,
                non_retryable=True,
            )

        found = await workflow.execute_activity_method(
            TopicPlannerActivities.find_plan,
            FindPlanRequest(
                plan_date=request.plan_date,
                strategy_profile_id=request.strategy_profile_id,
                content_profile_id=request.content_profile_id,
            ),
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )
        if found.topic_plan_id is not None:
            return TopicPlannerResult(
                topic_plan_id=found.topic_plan_id,
                topic=found.topic or "",
                reused=True,
                analytics_mode=found.analytics_mode or "",
            )

        context = await workflow.execute_activity_method(
            TopicPlannerActivities.gather_context,
            request,
            start_to_close_timeout=CONTEXT_ACTIVITY_TIMEOUT,
            retry_policy=STATE_RETRY_POLICY,
        )

        avoid: list[str] = []
        max_rounds = DEFAULT_PLANNER_POLICY.max_rounds
        for round_number in range(1, max_rounds + 1):
            try:
                generated = await workflow.execute_activity_method(
                    TopicPlannerActivities.generate_candidates,
                    GenerateCandidatesRequest(
                        context=context, round=round_number, avoid_subjects=list(avoid)
                    ),
                    start_to_close_timeout=GENERATE_ACTIVITY_TIMEOUT,
                    retry_policy=GENERATE_RETRY_POLICY,
                )
            except ActivityError as err:
                if not self._worth_another_round(err):
                    raise  # needs_input / permanent は繰り返しても変わらない
                continue

            saved = await workflow.execute_activity_method(
                TopicPlannerActivities.select_and_save,
                SelectAndSaveRequest(
                    context=context,
                    candidates=generated.candidates,
                    prompt_version=generated.prompt_version,
                    round=round_number,
                ),
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                retry_policy=STATE_RETRY_POLICY,
            )
            if saved.topic_plan_id is not None:
                return TopicPlannerResult(
                    topic_plan_id=saved.topic_plan_id,
                    topic=saved.topic or "",
                    reused=saved.reused,
                    analytics_mode=context.analytics.mode,
                )
            for subject in saved.rejected_subjects:
                if subject not in avoid:
                    avoid.append(subject)

        raise ApplicationError(
            f"no acceptable topic after {max_rounds} rounds for {request.plan_date}",
            type=TopicPlanningExhaustedError.__name__,
            non_retryable=True,
        )

    @staticmethod
    def _worth_another_round(err: ActivityError) -> bool:
        cause = err.cause
        type_name = cause.type if isinstance(cause, ApplicationError) else None
        return failure_class_from_type_name(type_name) in {
            FailureClass.TRANSIENT,
            FailureClass.RETRYABLE,
        }
