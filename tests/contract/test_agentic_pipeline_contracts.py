"""Agentic 実行の名前・queue・入出力の契約（ADR-0043 §6）。"""

from __future__ import annotations

import dataclasses

from contracts import agentic_pipeline as ap
from contracts.pipeline import (
    EPISODE_PIPELINE_WORKFLOW,
    PIPELINE_CONTROL_GATE,
    PIPELINE_TASK_QUEUE,
    PIPELINE_UPLOAD_GATE,
)


def test_agentic_workflows_do_not_reuse_the_legacy_pipeline_names() -> None:
    """日次の EpisodePipelineWorkflow を書き換えず別名にする（replay 互換 / INV-55）。"""
    names = {ap.AGENTIC_EPISODE_WORKFLOW[0], ap.AGENTIC_LEARNING_WORKFLOW[0]}
    assert EPISODE_PIPELINE_WORKFLOW[0] not in names
    assert len(names) == 2
    assert ap.AGENTIC_EPISODE_WORKFLOW[1] == PIPELINE_TASK_QUEUE


def test_activity_names_are_unique_and_agents_have_their_own_queue() -> None:
    activities = [
        ap.PIN_WORKFLOW_PLAN_ACTIVITY,
        ap.ADOPT_STRATEGY_CANDIDATE_ACTIVITY,
        ap.RUN_AGENT_STAGE_ACTIVITY,
        PIPELINE_CONTROL_GATE,
        PIPELINE_UPLOAD_GATE,
    ]
    assert len(set(activities)) == len(activities)
    assert ap.AGENTS_TASK_QUEUE != PIPELINE_TASK_QUEUE


def test_ids_and_idempotency_keys_are_deterministic() -> None:
    assert ap.agentic_episode_workflow_id("e") == "agentic-episode-e"
    assert ap.agentic_learning_workflow_id("e") == "agentic-learning-e"
    key = ap.agent_stage_idempotency_key("e", "strategy", "h")
    assert key == ap.agent_stage_idempotency_key("e", "strategy", "h") == "agentic:e:strategy:h"


def test_payloads_are_plain_dataclasses_for_the_temporal_converter() -> None:
    for cls in (
        ap.AgenticEpisodeInput,
        ap.PinWorkflowPlanRequest,
        ap.PinWorkflowPlanResult,
        ap.RunAgentStageRequest,
        ap.RunAgentStageResult,
        ap.AdoptStrategyCandidateRequest,
        ap.AdoptStrategyCandidateResult,
        ap.AgenticEpisodeResult,
        ap.AgenticLearningInput,
        ap.AgenticLearningResult,
    ):
        assert dataclasses.is_dataclass(cls), cls
