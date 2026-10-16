"""``PipelineOptions`` と agentic 起動の仕様を設定から作る唯一の場所（ADR-0044 §3-2）。

日次 Schedule の入力・介入 worker の再開・API の再開・CLI の初回起動が、同じ関数から
同じ ``(workflow 名, task queue, 入力)`` を得る。別々に組み立てると設定を変えた環境で
経路ごとに queue や並行枠が食い違う（AGENTS §8）。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from contracts.agentic_pipeline import AGENTIC_EPISODE_WORKFLOW, AgenticEpisodeInput
from contracts.pipeline import PipelineOptions, ProductionParameters
from infrastructure.config import Settings


def pipeline_options_from_settings(settings: Settings) -> PipelineOptions:
    """production の枠・render profile を設定から作る。"""
    return PipelineOptions(
        render_profile_id=settings.pipeline_render_profile_id,
        production=ProductionParameters(
            image_concurrency=settings.image_concurrency,
            video_concurrency=settings.video_concurrency,
            voice_concurrency=settings.voice_concurrency,
            image_max_rounds=settings.production_image_max_rounds,
            video_max_rounds=settings.production_video_max_rounds,
            await_reexecutions=settings.production_await_reexecutions,
            max_scene_alternatives_per_scene=(settings.production_max_scene_alternatives_per_scene),
        ),
    )


@dataclass(frozen=True, slots=True)
class AgenticStartSpec:
    workflow_name: str
    task_queue: str
    input: AgenticEpisodeInput

    def input_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self.input)


def agentic_episode_start_spec(
    *, episode_id: str, template_id: str, template_version: str, options: PipelineOptions
) -> AgenticStartSpec:
    """``AgenticEpisodeWorkflow`` の起動仕様。task queue は ``options.pipeline_task_queue``。"""
    return AgenticStartSpec(
        workflow_name=AGENTIC_EPISODE_WORKFLOW[0],
        task_queue=options.pipeline_task_queue,
        input=AgenticEpisodeInput(
            episode_id=episode_id,
            template_id=template_id,
            template_version=template_version,
            options=options,
        ),
    )
