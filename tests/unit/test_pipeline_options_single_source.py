"""再開設定の一本化（ADR-0044 §3-2）。各テストの理由: docs/testing/jpmas2-p2a-fixes-rationale.md"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

from contracts.agentic_pipeline import AGENTIC_EPISODE_WORKFLOW, AgenticEpisodeInput
from contracts.pipeline import PipelineOptions, ProductionParameters
from infrastructure.config import Settings
from infrastructure.temporal.agentic_starter import TemporalAgenticStarter
from infrastructure.temporal.pipeline_options import (
    agentic_episode_start_spec,
    pipeline_options_from_settings,
)
from infrastructure.temporal.schedules import daily_episode_input_from_settings
from workers.intervention.activities import InterventionActivities

TEMPLATE = ("shorts_agentic", "1")


def _settings() -> Settings:
    return Settings(
        image_concurrency=7,
        video_concurrency=3,
        voice_concurrency=5,
        production_image_max_rounds=4,
        production_video_max_rounds=5,
        production_await_reexecutions=2,
        pipeline_render_profile_id="custom_profile",
    )


def _legacy_daily_options(settings: Settings) -> PipelineOptions:
    """変更前の ``daily_episode_input_from_settings`` が直接組んでいた値（そのままの写し）。"""
    return PipelineOptions(
        render_profile_id=settings.pipeline_render_profile_id,
        production=ProductionParameters(
            image_concurrency=settings.image_concurrency,
            video_concurrency=settings.video_concurrency,
            voice_concurrency=settings.voice_concurrency,
            image_max_rounds=settings.production_image_max_rounds,
            video_max_rounds=settings.production_video_max_rounds,
            await_reexecutions=settings.production_await_reexecutions,
            max_scene_alternatives_per_scene=settings.production_max_scene_alternatives_per_scene,
        ),
    )


def test_daily_schedule_input_is_unchanged_by_the_extraction() -> None:
    for settings in (Settings(), _settings()):
        daily = daily_episode_input_from_settings(settings)
        assert dataclasses.asdict(daily.options) == dataclasses.asdict(
            _legacy_daily_options(settings)
        )
        assert daily.options == pipeline_options_from_settings(settings)


def test_the_task_queue_is_the_options_queue() -> None:
    options = PipelineOptions(pipeline_task_queue="custom-pipeline")
    spec = agentic_episode_start_spec(
        episode_id="e1", template_id="t", template_version="1", options=options
    )
    assert spec.workflow_name == AGENTIC_EPISODE_WORKFLOW[0]
    assert spec.task_queue == "custom-pipeline"
    assert spec.input_dict()["options"]["pipeline_task_queue"] == "custom-pipeline"
    default = agentic_episode_start_spec(
        episode_id="e1", template_id="t", template_version="1", options=PipelineOptions()
    )
    assert default.task_queue == AGENTIC_EPISODE_WORKFLOW[1]  # 既定は従来の定数と同じ


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    async def start_workflow(self, name: str, arg: Any, *, id: str, task_queue: str, **_: Any):
        payload: dict[str, Any] = (
            dataclasses.asdict(arg) if isinstance(arg, AgenticEpisodeInput) else dict(arg)
        )
        self.calls.append((name, payload, task_queue))


async def test_api_cli_and_worker_paths_build_the_same_start(
    session_factory, artifact_store, monkeypatch
) -> None:
    from apps.api.workflow_starter import TemporalWorkflowStarter

    settings = _settings()
    episode_id = str(uuid.uuid4())

    # API
    api_client = _RecordingClient()
    await TemporalWorkflowStarter(api_client, "q", settings).start_agentic_workflow(  # type: ignore[arg-type]
        episode_id=episode_id, template_id=TEMPLATE[0], template_version=TEMPLATE[1]
    )
    # CLI（初回起動）
    cli_client = _RecordingClient()
    await TemporalAgenticStarter(cli_client, pipeline_options_from_settings(settings)).start(  # type: ignore[arg-type]
        episode_id=uuid.UUID(episode_id), template_id=TEMPLATE[0], template_version=TEMPLATE[1]
    )

    # 介入 worker
    class _Pinned:
        template_id, template_version = TEMPLATE

    async def _read(*_a: Any, **_k: Any) -> _Pinned:
        return _Pinned()

    monkeypatch.setattr("workers.intervention.activities.read_pinned_agentic_template", _read)
    from infrastructure.intervention.budget import BudgetPolicy

    acts = InterventionActivities(
        session_factory=session_factory,
        store=artifact_store,
        budget_policy=BudgetPolicy(),
        pipeline_options=pipeline_options_from_settings(settings),
    )
    response: dict[str, Any] = {}
    await acts._choose_pipeline(response, episode_id)

    api, cli = api_client.calls[0], cli_client.calls[0]
    worker = (
        AGENTIC_EPISODE_WORKFLOW[0],
        response["pipeline_input"],
        response["pipeline_task_queue"],
    )
    assert api == cli == worker
    assert api[1]["options"]["production"]["image_concurrency"] == 7


async def test_a_custom_queue_reaches_every_path(
    session_factory, artifact_store, monkeypatch
) -> None:
    from apps.api.workflow_starter import TemporalWorkflowStarter
    from infrastructure.intervention.budget import BudgetPolicy

    settings = _settings()
    options = dataclasses.replace(
        pipeline_options_from_settings(settings), pipeline_task_queue="custom-pipeline"
    )
    monkeypatch.setattr(
        "apps.api.workflow_starter.pipeline_options_from_settings", lambda _s: options
    )
    episode_id = str(uuid.uuid4())
    api_client = _RecordingClient()
    await TemporalWorkflowStarter(api_client, "q", settings).start_agentic_workflow(  # type: ignore[arg-type]
        episode_id=episode_id, template_id=TEMPLATE[0], template_version=TEMPLATE[1]
    )

    class _Pinned:
        template_id, template_version = TEMPLATE

    async def _read(*_a: Any, **_k: Any) -> _Pinned:
        return _Pinned()

    monkeypatch.setattr("workers.intervention.activities.read_pinned_agentic_template", _read)
    acts = InterventionActivities(
        session_factory=session_factory,
        store=artifact_store,
        budget_policy=BudgetPolicy(),
        pipeline_options=options,
    )
    response: dict[str, Any] = {}
    await acts._choose_pipeline(response, episode_id)
    assert api_client.calls[0][2] == response["pipeline_task_queue"] == "custom-pipeline"
    assert api_client.calls[0][1] == response["pipeline_input"]
