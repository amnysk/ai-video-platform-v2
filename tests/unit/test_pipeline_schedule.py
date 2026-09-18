"""Daily Schedule の定義（ADR-0023）と pipeline 設定。サーバには繋がない（登録は integration）。"""

from __future__ import annotations

from datetime import timedelta

import pytest
from temporalio.client import ScheduleActionStartWorkflow, ScheduleOverlapPolicy

from contracts.pipeline import (
    DAILY_SCHEDULE_ID,
    DEFAULT_DAILY_EPISODE_LIMIT,
    DEFAULT_DAILY_SCHEDULE_CRON,
    DEFAULT_SCHEDULE_TIMEZONE,
    DailyEpisodeInput,
    PipelineOptions,
)
from contracts.render import DEFAULT_RENDER_PROFILE_ID
from contracts.topic_planning import DEFAULT_CONTENT_PROFILE_ID, DEFAULT_STRATEGY_PROFILE_ID
from infrastructure.config import Settings
from infrastructure.temporal.schedules import (
    build_daily_episode_schedule,
    daily_episode_input_from_settings,
    describe_daily_schedule_spec,
)


def test_schedule_starts_daily_workflow_on_pipeline_queue_with_skip_overlap() -> None:
    wf_input = DailyEpisodeInput(daily_limit=1, options=PipelineOptions())
    schedule = build_daily_episode_schedule(
        cron="0 6 * * *", timezone="Asia/Tokyo", workflow_input=wf_input, paused=True
    )
    action = schedule.action
    assert isinstance(action, ScheduleActionStartWorkflow)
    assert action.workflow == "DailyEpisodeWorkflow"
    assert action.task_queue == "pipeline"
    assert action.id == "daily-episode"
    assert list(action.args) == [wf_input]
    assert schedule.spec.cron_expressions == ["0 6 * * *"]
    assert schedule.spec.time_zone_name == "Asia/Tokyo"
    assert schedule.policy.overlap == ScheduleOverlapPolicy.SKIP
    assert schedule.policy.catchup_window == timedelta(hours=1)
    assert schedule.state.paused is True


def test_schedule_task_queue_follows_the_input() -> None:
    wf_input = DailyEpisodeInput(options=PipelineOptions(pipeline_task_queue="pipeline-x"))
    schedule = build_daily_episode_schedule(
        cron="0 6 * * *", timezone="UTC", workflow_input=wf_input, task_queue="pipeline-x"
    )
    assert isinstance(schedule.action, ScheduleActionStartWorkflow)
    assert schedule.action.task_queue == "pipeline-x"


def test_invalid_timezone_is_rejected_before_registration() -> None:
    with pytest.raises(ValueError):
        build_daily_episode_schedule(
            cron="0 6 * * *", timezone="Nowhere/Invalid", workflow_input=DailyEpisodeInput()
        )


def test_settings_defaults_and_input_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "DAILY_EPISODE_LIMIT",
        "DAILY_SCHEDULE_CRON",
        "SCHEDULE_TIMEZONE",
        "TOPIC_STRATEGY_PROFILE_ID",
        "TOPIC_CONTENT_PROFILE_ID",
        "YOUTUBE_ANALYTICS_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert settings.daily_episode_limit == DEFAULT_DAILY_EPISODE_LIMIT == 1
    assert settings.daily_schedule_cron == DEFAULT_DAILY_SCHEDULE_CRON == "0 6 * * *"
    assert settings.schedule_timezone == DEFAULT_SCHEDULE_TIMEZONE == "Asia/Tokyo"
    assert settings.pipeline_render_profile_id == DEFAULT_RENDER_PROFILE_ID
    assert settings.daily_schedule_id == DAILY_SCHEDULE_ID
    assert settings.topic_strategy_profile_id == DEFAULT_STRATEGY_PROFILE_ID
    assert settings.topic_content_profile_id == DEFAULT_CONTENT_PROFILE_ID
    assert settings.youtube_analytics_enabled is False

    monkeypatch.setenv("PIPELINE_RENDER_PROFILE_ID", "long_horizontal")
    monkeypatch.setenv("IMAGE_CONCURRENCY", "3")
    wf_input = daily_episode_input_from_settings(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    assert wf_input.options.render_profile_id == "long_horizontal"
    assert wf_input.options.production.image_concurrency == 3
    assert wf_input.daily_limit == 1
    assert wf_input.timezone == "Asia/Tokyo"
    assert wf_input.strategy_profile_id == DEFAULT_STRATEGY_PROFILE_ID
    assert wf_input.content_profile_id == DEFAULT_CONTENT_PROFILE_ID

    monkeypatch.setenv("TOPIC_CONTENT_PROFILE_ID", "long_form")
    wf_input = daily_episode_input_from_settings(Settings(_env_file=None))  # pyright: ignore[reportCallIssue]
    assert wf_input.content_profile_id == "long_form"


@pytest.mark.parametrize("env", ["TOPIC_STRATEGY_PROFILE_ID", "TOPIC_CONTENT_PROFILE_ID"])
def test_unknown_topic_profile_ids_are_rejected(monkeypatch: pytest.MonkeyPatch, env: str) -> None:
    from pydantic import ValidationError

    monkeypatch.setenv(env, "no_such_profile")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_describe_spec_is_printable_without_secrets() -> None:
    text = describe_daily_schedule_spec(
        schedule_id="avp-daily-episode",
        cron="0 6 * * *",
        timezone="Asia/Tokyo",
        workflow_input=DailyEpisodeInput(),
        paused=False,
    )
    assert "avp-daily-episode" in text
    assert "DailyEpisodeWorkflow" in text
    assert "shorts_vertical" in text
