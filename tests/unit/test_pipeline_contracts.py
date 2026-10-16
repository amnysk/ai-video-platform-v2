"""Pipeline の契約（ADR-0023）: workflow 名・id 規約・slot_date の導出。"""

from __future__ import annotations

from datetime import UTC, date, datetime

from apps.api import workflow_starter
from contracts.pipeline import (
    DAILY_EPISODE_WORKFLOW,
    DAILY_SCHEDULE_ID,
    EPISODE_PIPELINE_WORKFLOW,
    PIPELINE_TASK_QUEUE,
    STAGE_PARKING_STATUS,
    PipelineOptions,
    PipelineStage,
    local_slot_date,
    pipeline_workflow_id,
    production_workflow_id,
    render_workflow_id,
    script_workflow_id,
    storyboard_workflow_id,
    upload_workflow_id,
)
from contracts.render import DEFAULT_RENDER_PROFILE_ID
from contracts.states import EpisodeStatus


def test_names_and_queue() -> None:
    assert DAILY_EPISODE_WORKFLOW == ("DailyEpisodeWorkflow", "pipeline")
    assert EPISODE_PIPELINE_WORKFLOW == ("EpisodePipelineWorkflow", "pipeline")
    assert PIPELINE_TASK_QUEUE == "pipeline"
    assert DAILY_SCHEDULE_ID == "avp-daily-episode"


def test_workflow_ids_match_the_api_starter_convention() -> None:
    """API から起動した工程と pipeline から起動した工程は同じ id を取り合う（二重起動しない）。"""
    ep = "0f0e"
    assert script_workflow_id(ep) == f"episode-{ep}"
    assert storyboard_workflow_id(ep) == workflow_starter.storyboard_workflow_id(ep)
    assert production_workflow_id(ep) == workflow_starter.production_workflow_id(ep)
    assert render_workflow_id(ep) == workflow_starter.render_workflow_id(ep)
    assert upload_workflow_id(ep) == workflow_starter.upload_workflow_id(ep)
    assert pipeline_workflow_id(ep) == f"episode-{ep}-pipeline"


def test_stage_order_and_parking_states() -> None:
    assert list(PipelineStage) == ["script", "storyboard", "production", "render", "upload"]
    assert STAGE_PARKING_STATUS == {
        PipelineStage.SCRIPT: EpisodeStatus.SCRIPT_READY,
        PipelineStage.STORYBOARD: EpisodeStatus.STORYBOARD_READY,
        PipelineStage.PRODUCTION: EpisodeStatus.ASSETS_READY,
        PipelineStage.RENDER: EpisodeStatus.RENDER_READY,
        PipelineStage.UPLOAD: EpisodeStatus.UPLOADED,
    }


def test_render_profile_is_carried_not_hardcoded() -> None:
    assert PipelineOptions().render_profile_id == DEFAULT_RENDER_PROFILE_ID
    assert PipelineOptions(render_profile_id="long_horizontal").render_profile_id == (
        "long_horizontal"
    )


def test_local_slot_date_uses_the_configured_timezone() -> None:
    # 2026-09-15 21:30 UTC は東京では 9/16 06:30
    instant = datetime(2026, 9, 15, 21, 30, tzinfo=UTC)
    assert local_slot_date(instant, "Asia/Tokyo") == date(2026, 9, 16)
    assert local_slot_date(instant, "UTC") == date(2026, 9, 15)


def test_busy_wait_budget_covers_the_planner_execution_timeout() -> None:
    """走行中の Planner が timeout で終わるまで Daily が待ち切れる（予算は timeout から導く）。"""
    from contracts.pipeline import TOPIC_PLANNER_BUSY_WAIT_SECONDS, TOPIC_PLANNER_START_ATTEMPTS
    from contracts.topic_planning import TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS

    waited = (TOPIC_PLANNER_START_ATTEMPTS - 1) * TOPIC_PLANNER_BUSY_WAIT_SECONDS
    assert waited >= TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS


def test_candidate_batch_bounds_come_from_the_planner_policy() -> None:
    from contracts.topic_planning import DEFAULT_PLANNER_POLICY, TopicCandidateBatch

    schema = TopicCandidateBatch.model_json_schema()["properties"]["candidates"]
    assert schema["minItems"] == DEFAULT_PLANNER_POLICY.candidate_count_min
    assert schema["maxItems"] == DEFAULT_PLANNER_POLICY.candidate_count_max
