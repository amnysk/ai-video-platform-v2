"""Temporal workflow の起動だけを担う薄い層。

APIは workflow の**完了を待たない**（INV-16）。start して即座に返す。
"""

from __future__ import annotations

from typing import Protocol

from temporalio.client import Client

from contracts.states import (
    PIPELINE_WORKFLOWS,
    PRODUCTION_WORKFLOW,
    STORYBOARD_WORKFLOW,
    Pipeline,
)
from infrastructure.config import Settings

#: Settings に値が無いときの既定（workflow の入力 dataclass の既定と揃える / ADR-0017）
DEFAULT_IMAGE_MAX_ROUNDS = 3
DEFAULT_VIDEO_MAX_ROUNDS = 2
DEFAULT_AWAIT_REEXECUTIONS = 3


class WorkflowStarter(Protocol):
    async def start_episode_workflow(
        self, *, episode_id: str, pipeline: Pipeline | str = Pipeline.SKELETON
    ) -> str: ...

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        """既存 Episode に対して StoryboardWorkflow を起動する（ADR-0015）。"""
        ...

    async def start_production_workflow(self, *, episode_id: str) -> str:
        """既存 Episode に対して ProductionWorkflow を起動する（ADR-0017）。"""
        ...


class TemporalWorkflowStarter:
    def __init__(self, client: Client, task_queue: str, settings: Settings | None = None) -> None:
        self._client = client
        #: production の workflow 側の並行枠（worker の並行数設定と揃える / ADR-0017）
        self._settings = settings or Settings()
        #: 骨組みworkflowの既定 queue。他は PIPELINE_WORKFLOWS から引く。
        self._task_queue = task_queue

    @classmethod
    async def connect(cls, settings: Settings) -> TemporalWorkflowStarter:
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        return cls(client, settings.temporal_task_queue, settings)

    async def start_episode_workflow(
        self, *, episode_id: str, pipeline: Pipeline | str = Pipeline.SKELETON
    ) -> str:
        selected = Pipeline(pipeline)
        workflow_name, task_queue = PIPELINE_WORKFLOWS[selected]
        if selected is Pipeline.SKELETON:
            task_queue = self._task_queue
        workflow_id = f"episode-{episode_id}"
        # workflow定義そのものはworkerが持つ。APIは名前と引数だけを知る（INV-1 / INV-3）。
        # **完了は待たない**（INV-16）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        workflow_name, task_queue = STORYBOARD_WORKFLOW
        workflow_id = storyboard_workflow_id(episode_id)
        # 同じ id の実行が走っていれば Temporal が拒否する（二重起動しない）。
        # 完了済みの id は再利用できる（再実行は activity 側の skip 判定で冪等 / INV-17）。
        await self._client.start_workflow(
            workflow_name,
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id

    async def start_production_workflow(self, *, episode_id: str) -> str:
        workflow_name, task_queue = PRODUCTION_WORKFLOW
        workflow_id = production_workflow_id(episode_id)
        # 入力は workflow の dataclass と同じ形の dict（worker の型を import しない / INV-3）。
        await self._client.start_workflow(
            workflow_name,
            {
                "episode_id": episode_id,
                "image_concurrency": self._settings.image_concurrency,
                "video_concurrency": self._settings.video_concurrency,
                "voice_concurrency": self._settings.voice_concurrency,
                "image_max_rounds": getattr(
                    self._settings, "production_image_max_rounds", DEFAULT_IMAGE_MAX_ROUNDS
                ),
                "video_max_rounds": getattr(
                    self._settings, "production_video_max_rounds", DEFAULT_VIDEO_MAX_ROUNDS
                ),
                "await_reexecutions": getattr(
                    self._settings, "production_await_reexecutions", DEFAULT_AWAIT_REEXECUTIONS
                ),
            },
            id=workflow_id,
            task_queue=task_queue,
        )
        return workflow_id


def production_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-production"


def storyboard_workflow_id(episode_id: str) -> str:
    return f"episode-{episode_id}-storyboard"
