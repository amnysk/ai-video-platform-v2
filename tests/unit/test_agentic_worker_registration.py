"""pipeline worker の登録: agentic は flag 有効時だけ（ADR-0043 §5、INV-55）。

各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import inspect

from contracts.agentic_pipeline import AGENTIC_PIPELINE_ACTIVITY_NAMES
from contracts.pipeline import PIPELINE_ACTIVITY_NAMES
from workers.pipeline import run_worker
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.agentic_activities import AgenticPipelineActivities
from workers.pipeline.agentic_workflow import AgenticEpisodeWorkflow, AgenticLearningWorkflow
from workers.pipeline.watchdog import DailyWatchdogWorkflow
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow


def _names(acts: list) -> set[str]:
    return {fn.__temporal_activity_definition.name for fn in acts}  # type: ignore[attr-defined]


def _pipeline(session_factory) -> PipelineActivities:
    return PipelineActivities(
        session_factory=session_factory, paused_env=False, uploads_paused_env=False
    )


def test_the_default_registration_is_exactly_the_daily_workflows_and_activities(
    session_factory,
) -> None:
    workflows, acts = run_worker.registrations(_pipeline(session_factory))
    assert workflows == [DailyEpisodeWorkflow, EpisodePipelineWorkflow, DailyWatchdogWorkflow]
    assert _names(acts) == set(PIPELINE_ACTIVITY_NAMES)


def test_the_agentic_registration_adds_only_the_agentic_workflows_and_activities(
    session_factory, artifact_store
) -> None:
    agentic = AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    )
    workflows, acts = run_worker.registrations(_pipeline(session_factory), agentic)
    assert workflows == [
        DailyEpisodeWorkflow,
        EpisodePipelineWorkflow,
        DailyWatchdogWorkflow,
        AgenticEpisodeWorkflow,
        AgenticLearningWorkflow,
    ]
    assert _names(acts) == set(PIPELINE_ACTIVITY_NAMES) | set(AGENTIC_PIPELINE_ACTIVITY_NAMES)


def test_the_agentic_activities_are_exactly_the_contract_names(
    session_factory, artifact_store
) -> None:
    agentic = AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    )
    assert _names(list(agentic.activities())) == set(AGENTIC_PIPELINE_ACTIVITY_NAMES)


def test_main_builds_the_agentic_activities_only_behind_the_flag() -> None:
    source = inspect.getsource(run_worker.main)
    assert "if settings.jpmas2_agentic_workflows:" in source
    assert source.index("AgenticPipelineActivities(") > source.index(
        "if settings.jpmas2_agentic_workflows:"
    )
