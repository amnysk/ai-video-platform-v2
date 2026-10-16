"""AgenticEpisodeWorkflow / AgenticLearningWorkflow（ADR-0043 §6）。I/O をしない。

``EpisodePipelineWorkflow`` は**書き換えない**（replay 互換 / INV-55）。この workflow は新しい
workflow type で、既存工程は同じ子 workflow・同じ id 規則・同じ ``STAGE_PARKING_STATUS``・同じ
control gate / upload gate Activity を使う（``workers.pipeline.workflows`` の helper を使う）。

    pin_workflow_plan（Activity）→ その結果（履歴）にある計画だけを使う（INV-51）
    計画の stage を順に:
        control gate（停止中・待機中なら parked で終わる）
        agent stage        → run_agent_stage（queue ``agents``）
        existing_producer_adopt → adopt_strategy_candidate
        existing_*         → 既存の子 workflow（private_upload の前に upload gate）
    blocked / rejected_output / 失敗は理由を残して止まる（黙って飛ばさない）。後ろ向きの辺は無い。

QA の自動修正はしない（提案だけ）。``quality`` は計画に1度しか現れず、1度だけ走る。
Learning は別 workflow（``AgenticLearningWorkflow``、明示起動）。
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ChildWorkflowError,
    WorkflowAlreadyStartedError,
)
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from contracts.agentic import PlannedStage, WorkflowPlan
    from contracts.agentic_pipeline import (
        ADOPT_STRATEGY_CANDIDATE_ACTIVITY,
        AGENT_SPEC_DRIFT_ERROR,
        AGENTIC_EPISODE_WORKFLOW,
        AGENTIC_LEARNING_WORKFLOW,
        AGENTS_TASK_QUEUE,
        EXISTING_HANDLER_STAGE,
        LOAD_WORKFLOW_PLAN_ACTIVITY,
        NON_PIPELINE_GATE_STAGE,
        PIN_WORKFLOW_PLAN_ACTIVITY,
        RUN_AGENT_STAGE_ACTIVITY,
        AdoptStrategyCandidateRequest,
        AdoptStrategyCandidateResult,
        AgenticEpisodeInput,
        AgenticEpisodeResult,
        AgenticLearningInput,
        AgenticLearningResult,
        AgenticOutcome,
        AgentStageOutcome,
        LoadWorkflowPlanRequest,
        PinWorkflowPlanRequest,
        PinWorkflowPlanResult,
        RunAgentStageRequest,
        RunAgentStageResult,
    )
    from contracts.intervention import AgentRole
    from contracts.log_contract import EventName, Outcome
    from contracts.pipeline import (
        PIPELINE_CONTROL_GATE,
        PIPELINE_UPLOAD_GATE,
        STAGE_PARKING_STATUS,
        ControlGateRequest,
        ControlGateResult,
        PipelineStage,
        UploadGateRequest,
        UploadGateResult,
    )
    from contracts.workflow_templates import ExistingStageHandler
    from workers.pipeline.workflows import _event, _stage_request, _state_activity

_PRODUCER = ExistingStageHandler.PRODUCER_ADOPT.value


def _cause_kind(err: BaseException) -> str:
    """原因の型だけ（例外文は写さない / INV-20）。"""
    inner = getattr(err, "cause", None)
    cause = inner if inner is not None else err
    kind = type(cause).__name__
    app_type = getattr(cause, "type", None)
    if app_type and app_type != kind:
        kind = f"{kind}({app_type})"
    return kind


async def _run_agent_stage(
    episode_id: str,
    stage: PlannedStage,
    plan: WorkflowPlan,
    retry_attempts: dict[str, int],
    idempotency_scope: str | None = None,
) -> RunAgentStageResult:
    assert stage.timeout_seconds is not None  # agent stage は固定済み（PlannedStage の検証）
    return await workflow.execute_activity(
        RUN_AGENT_STAGE_ACTIVITY,
        RunAgentStageRequest(
            episode_id=episode_id,
            stage=stage.model_dump(mode="json"),
            plan_hash=plan.plan_hash,
            template_id=plan.template_id,
            template_version=plan.template_version,
            idempotency_scope=idempotency_scope,
        ),
        result_type=RunAgentStageResult,
        task_queue=AGENTS_TASK_QUEUE,
        start_to_close_timeout=timedelta(seconds=stage.timeout_seconds),
        retry_policy=RetryPolicy(
            maximum_attempts=max(1, retry_attempts.get(stage.stage_id, 1)),
            non_retryable_error_types=[AGENT_SPEC_DRIFT_ERROR],
        ),
    )


@workflow.defn(name=AGENTIC_EPISODE_WORKFLOW[0])
class AgenticEpisodeWorkflow:
    @workflow.run
    async def run(self, request: AgenticEpisodeInput) -> AgenticEpisodeResult:
        ep = request.episode_id
        result = AgenticEpisodeResult(episode_id=ep, outcome=AgenticOutcome.COMPLETED)
        try:
            pinned: PinWorkflowPlanResult = await _state_activity(
                PIN_WORKFLOW_PLAN_ACTIVITY,
                PinWorkflowPlanRequest(
                    episode_id=ep,
                    template_id=request.template_id,
                    template_version=request.template_version,
                ),
                PinWorkflowPlanResult,
            )
            plan = WorkflowPlan.model_validate(pinned.plan)
        except ActivityError as err:
            return self._stop(result, None, f"plan not pinned: {_cause_kind(err)}")
        result.plan_hash = plan.plan_hash
        _event(
            logging.INFO,
            EventName.STAGE_STARTED,
            "agentic episode started plan=%s",
            plan.plan_hash,
            stage="pipeline",
            episode_id=ep,
            outcome=Outcome.STARTED.value,
            attributes={
                "template_id": plan.template_id,
                "template_version": plan.template_version,
                "plan_hash": plan.plan_hash,
                "existing_plan": pinned.existing,
            },
        )

        for stage in plan.stages:
            gate_stage = EXISTING_HANDLER_STAGE.get(stage.handler, NON_PIPELINE_GATE_STAGE)
            control: ControlGateResult = await _state_activity(
                PIPELINE_CONTROL_GATE,
                ControlGateRequest(episode_id=ep, stage=gate_stage.value),
                ControlGateResult,
            )
            if not control.proceed:
                return self._park(result, stage, control)

            if stage.kind == "agent":
                try:
                    ran = await _run_agent_stage(ep, stage, plan, pinned.retry_max_attempts)
                except ActivityError as err:
                    return self._stop(result, stage, f"agent stage failed: {_cause_kind(err)}")
                if ran.outcome != AgentStageOutcome.PRODUCED.value:
                    return self._stop(
                        result, stage, f"agent stage {ran.outcome}: {ran.reason or 'no reason'}"
                    )
                # QA は提案だけ。自動適用も修復の再実行もしない（後ろ向きの辺は無い）
                if stage.agent_role is AgentRole.QUALITY_REVIEWER:
                    result.qa_repair_cycles += len(ran.proposal_command_ids)
            elif stage.handler == _PRODUCER:
                adopted: AdoptStrategyCandidateResult = await _state_activity(
                    ADOPT_STRATEGY_CANDIDATE_ACTIVITY,
                    AdoptStrategyCandidateRequest(
                        episode_id=ep, stage_id=stage.stage_id, plan_hash=plan.plan_hash
                    ),
                    AdoptStrategyCandidateResult,
                )
                if not adopted.adopted:
                    return self._stop(
                        result, stage, f"no candidate adopted: {adopted.reason or 'no reason'}"
                    )
            elif stage.handler in EXISTING_HANDLER_STAGE:
                stopped = await self._run_existing(
                    result, stage, EXISTING_HANDLER_STAGE[stage.handler], request
                )
                if stopped is not None:
                    return stopped
            else:
                return self._stop(result, stage, f"unsupported handler {stage.handler!r}")
            result.completed_stages.append(stage.stage_id)

        _event(
            logging.INFO,
            EventName.STAGE_SUCCEEDED,
            "agentic episode completed",
            stage="pipeline",
            episode_id=ep,
            outcome=Outcome.SUCCEEDED.value,
            attributes={"completed_stages": list(result.completed_stages)},
        )
        return result

    async def _run_existing(
        self,
        result: AgenticEpisodeResult,
        planned: PlannedStage,
        stage: PipelineStage,
        request: AgenticEpisodeInput,
    ) -> AgenticEpisodeResult | None:
        """既存工程の子 workflow（``EpisodePipelineWorkflow`` と同じ id・queue・入力・駐機点）。"""
        ep = request.episode_id
        if stage is PipelineStage.UPLOAD:
            gate: UploadGateResult = await _state_activity(
                PIPELINE_UPLOAD_GATE, UploadGateRequest(episode_id=ep), UploadGateResult
            )
            if not gate.allowed:
                _event(
                    logging.INFO,
                    EventName.UPLOAD_SKIPPED,
                    "upload skipped episode=%s: %s",
                    ep,
                    gate.reason,
                    stage=stage.value,
                    episode_id=ep,
                    outcome=Outcome.SKIPPED.value,
                    attributes={"reason": gate.reason, "status": gate.status},
                )
                return self._stop(result, planned, f"upload skipped: {gate.reason}")
        (name, queue), child_id, payload = _stage_request(stage, ep, request.options)
        child: Any
        try:
            # 子の閉じ方は ABANDON: workflow を止めても有料の工程・投稿を途中で殺さない
            child = await workflow.execute_child_workflow(
                name,
                payload,
                id=child_id,
                task_queue=queue,
                result_type=dict,
                parent_close_policy=ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            return self._stop(result, planned, f"{child_id} is already running")
        except ChildWorkflowError as err:
            return self._stop(result, planned, f"{child_id} failed: {_cause_kind(err)}")
        status = str(child.get("status", "")) if isinstance(child, dict) else ""
        expected = STAGE_PARKING_STATUS[stage]
        if status != expected:
            return self._stop(result, planned, f"{name} returned {status!r}, expected {expected}")
        return None

    @staticmethod
    def _park(
        result: AgenticEpisodeResult, stage: PlannedStage, control: ControlGateResult
    ) -> AgenticEpisodeResult:
        """停止中・待機中で工程を起動しない。失敗ではない（再開は介入 command が行う）。"""
        _event(
            logging.INFO,
            EventName.STAGE_SKIPPED,
            "agentic episode parked before %s: %s",
            stage.stage_id,
            control.reason,
            stage="pipeline",
            episode_id=result.episode_id,
            outcome=Outcome.SKIPPED.value,
            attributes={
                "reason": "control_gate",
                "parked_stage": stage.stage_id,
                "control_state": control.control_state,
                "waiting_point": control.waiting_point,
                "manifest_id": control.manifest_id,
            },
        )
        result.outcome = AgenticOutcome.PARKED
        result.stopped_stage = stage.stage_id
        result.reason = control.reason
        return result

    @staticmethod
    def _stop(
        result: AgenticEpisodeResult, stage: PlannedStage | None, reason: str
    ) -> AgenticEpisodeResult:
        stage_id = stage.stage_id if stage is not None else None
        _event(
            logging.WARNING,
            EventName.STAGE_BLOCKED,
            "agentic episode stopped at %s: %s",
            stage_id,
            reason,
            stage="pipeline",
            episode_id=result.episode_id,
            outcome=Outcome.BLOCKED.value,
            attributes={"stopped_stage": stage_id, "reason": reason},
        )
        result.outcome = AgenticOutcome.STOPPED
        result.stopped_stage = stage_id
        result.reason = reason
        return result


@workflow.defn(name=AGENTIC_LEARNING_WORKFLOW[0])
class AgenticLearningWorkflow:
    """post_publish の Learning（明示起動）。新しい計画は pin しない。

    測定が無い・private のままなら ``awaiting_public_metrics`` をそのまま返す（Lesson を作らない）。
    """

    @workflow.run
    async def run(self, request: AgenticLearningInput) -> AgenticLearningResult:
        ep = request.episode_id
        try:
            loaded: PinWorkflowPlanResult = await _state_activity(
                LOAD_WORKFLOW_PLAN_ACTIVITY,
                LoadWorkflowPlanRequest(episode_id=ep),
                PinWorkflowPlanResult,
            )
            plan = WorkflowPlan.model_validate(loaded.plan)
        except ActivityError as err:
            return self._blocked(ep, f"plan not loaded: {_cause_kind(err)}")
        stage = next((s for s in plan.post_publish if s.kind == "agent"), None)
        if stage is None:
            return self._blocked(ep, "the pinned plan has no post_publish agent stage")
        try:
            # 起動ごとに新しい実行にする（測定が揃うまで何度でも起動できる）。再試行は同じ結果
            ran = await _run_agent_stage(
                ep, stage, plan, loaded.retry_max_attempts, workflow.info().run_id
            )
        except ActivityError as err:
            return self._blocked(ep, f"agent stage failed: {_cause_kind(err)}")
        return AgenticLearningResult(
            episode_id=ep,
            outcome=ran.outcome,
            lesson_revision_id=ran.artifact_revision_id,
            reason=ran.reason,
        )

    @staticmethod
    def _blocked(episode_id: str, reason: str) -> AgenticLearningResult:
        return AgenticLearningResult(
            episode_id=episode_id, outcome=AgentStageOutcome.BLOCKED.value, reason=reason
        )


__all__ = ["AgenticEpisodeWorkflow", "AgenticLearningWorkflow"]
