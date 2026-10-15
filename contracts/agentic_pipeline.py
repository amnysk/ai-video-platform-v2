"""Agentic 実行（ADR-0043 §6）の workflow / Activity の名前・task queue・入出力。

ここが**唯一の宣言元**（AGENTS.md §8）。workflow は実装を import せず、
名前と task queue で呼ぶ（INV-3）。
入出力は Temporal の既定 converter で運べる dataclass。計画（``contracts.agentic.WorkflowPlan``）は
JSON の dict として運び、受け手が ``WorkflowPlan.model_validate`` で検証する
（版を黙って読み替えない / INV-54）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from contracts.pipeline import PIPELINE_TASK_QUEUE, PipelineOptions, PipelineStage
from contracts.workflow_templates import ExistingStageHandler

#: Agent stage の Activity を処理する worker の task queue（``workers/agents``）
AGENTS_TASK_QUEUE = "agents"

AGENTIC_EPISODE_WORKFLOW: tuple[str, str] = ("AgenticEpisodeWorkflow", PIPELINE_TASK_QUEUE)
AGENTIC_LEARNING_WORKFLOW: tuple[str, str] = ("AgenticLearningWorkflow", PIPELINE_TASK_QUEUE)

#: pipeline worker の Activity
PIN_WORKFLOW_PLAN_ACTIVITY = "pin_workflow_plan"
ADOPT_STRATEGY_CANDIDATE_ACTIVITY = "adopt_strategy_candidate"
#: Episode に pin 済みの計画を読むだけ（pin しない）。Learning が使う
LOAD_WORKFLOW_PLAN_ACTIVITY = "load_workflow_plan"
#: pipeline worker の agentic Activity（flag 有効時だけ登録。既定の worker は持たない）
AGENTIC_PIPELINE_ACTIVITY_NAMES: tuple[str, ...] = (
    PIN_WORKFLOW_PLAN_ACTIVITY,
    ADOPT_STRATEGY_CANDIDATE_ACTIVITY,
    LOAD_WORKFLOW_PLAN_ACTIVITY,
)
#: agent worker の Activity
RUN_AGENT_STAGE_ACTIVITY = "run_agent_stage"
#: pin された spec の hash と Registry の hash が違う（INV-51）。再試行しない
AGENT_SPEC_DRIFT_ERROR = "agent_spec_drift"


#: 既存工程の handler -> 既存 pipeline の工程。子 workflow の名前・id・入力は ``_stage_request`` が
#: この工程から作る（``EpisodePipelineWorkflow`` と同じ）。``existing_producer_adopt`` は含めない
EXISTING_HANDLER_STAGE: dict[str, PipelineStage] = {
    ExistingStageHandler.SCRIPT.value: PipelineStage.SCRIPT,
    ExistingStageHandler.STORYBOARD.value: PipelineStage.STORYBOARD,
    ExistingStageHandler.PRODUCTION.value: PipelineStage.PRODUCTION,
    ExistingStageHandler.RENDER.value: PipelineStage.RENDER,
    ExistingStageHandler.PRIVATE_UPLOAD.value: PipelineStage.UPLOAD,
}

#: agent stage・producer 採用の前後で control gate（停止・待機）を問うときの工程。待機地点
#: （``STAGE_REVIEW_POINT``）を持たない先頭の工程なので、停止中かどうかだけが効く
NON_PIPELINE_GATE_STAGE = PipelineStage.SCRIPT


def agentic_episode_workflow_id(episode_id: str) -> str:
    return f"agentic-episode-{episode_id}"


def agentic_learning_workflow_id(episode_id: str) -> str:
    return f"agentic-learning-{episode_id}"


def agent_stage_idempotency_key(
    episode_id: str, stage_id: str, plan_hash: str, scope: str | None = None
) -> str:
    """stage 実行の冪等キー。``scope`` は Learning のように何度でも起動し直す stage が、起動ごとに
    新しい実行にする印（同じ起動の中の Temporal 再試行は同じ scope = 同じ結果）。"""
    key = f"agentic:{episode_id}:{stage_id}:{plan_hash}"
    return key if not scope else f"{key}:{scope}"


class AgentStageOutcome(StrEnum):
    PRODUCED = "produced"
    AWAITING_PUBLIC_METRICS = "awaiting_public_metrics"
    REJECTED_OUTPUT = "rejected_output"
    #: flag 無効（agent_disabled）・予算（budget_*）・停止中（held）。黙って飛ばさない
    BLOCKED = "blocked"


class AgenticOutcome(StrEnum):
    COMPLETED = "completed"
    PARKED = "parked"
    STOPPED = "stopped"


@dataclass
class AgenticEpisodeInput:
    episode_id: str
    template_id: str
    template_version: str
    options: PipelineOptions = field(default_factory=PipelineOptions)


@dataclass
class PinWorkflowPlanRequest:
    episode_id: str
    template_id: str
    template_version: str


@dataclass
class PinWorkflowPlanResult:
    #: ``WorkflowPlan`` の JSON（``model_dump(mode="json")``）
    plan: dict[str, Any]
    #: 既に pin されていた計画を返した
    existing: bool = False
    #: agent stage の ``stage_id`` -> pin した spec の ``retry_max_attempts``。計画には持たない値を
    #: Activity が Registry から引いて履歴に残す（workflow は Registry を読まない / INV-51）
    retry_max_attempts: dict[str, int] = field(default_factory=dict)


@dataclass
class LoadWorkflowPlanRequest:
    episode_id: str


@dataclass
class RunAgentStageRequest:
    episode_id: str
    #: ``PlannedStage`` の JSON（pin された stage。spec の版と hash を含む）
    stage: dict[str, Any]
    plan_hash: str
    template_id: str
    template_version: str
    #: 冪等キーの追加の印。None = 従来どおり（Episode の本流の stage）。Learning は起動ごとの run id
    idempotency_scope: str | None = None


@dataclass
class RunAgentStageResult:
    outcome: str  # AgentStageOutcome
    reason: str | None = None
    artifact_revision_id: str | None = None
    agent_run_id: str | None = None
    decision_id: str | None = None
    proposal_command_ids: list[str] = field(default_factory=list)
    #: replay で保存済みの結果を返した
    replayed: bool = False


@dataclass
class AdoptStrategyCandidateRequest:
    episode_id: str
    stage_id: str
    plan_hash: str


@dataclass
class AdoptStrategyCandidateResult:
    adopted: bool
    candidate_id: str | None = None
    decision_id: str | None = None
    reason: str | None = None


@dataclass
class AgenticEpisodeResult:
    episode_id: str
    outcome: str  # AgenticOutcome
    plan_hash: str | None = None
    completed_stages: list[str] = field(default_factory=list)
    stopped_stage: str | None = None
    reason: str | None = None
    #: QA の修正サイクル（P1 は自動修正しない。提案の数だけ記録する）
    qa_repair_cycles: int = 0


@dataclass
class AgenticLearningInput:
    episode_id: str


@dataclass
class AgenticLearningResult:
    episode_id: str
    outcome: str  # AgentStageOutcome
    lesson_revision_id: str | None = None
    reason: str | None = None
