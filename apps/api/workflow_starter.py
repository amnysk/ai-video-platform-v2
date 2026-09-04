"""Temporal workflow の起動だけを担う薄い層。

APIは workflow の**完了を待たない**（INV-16）。start して即座に返す。
"""

from __future__ import annotations

from typing import Protocol

from temporalio.client import Client

from infrastructure.config import Settings


class WorkflowStarter(Protocol):
    async def start_episode_workflow(self, *, episode_id: str) -> str: ...


class TemporalWorkflowStarter:
    def __init__(self, client: Client, task_queue: str) -> None:
        self._client = client
        self._task_queue = task_queue

    @classmethod
    async def connect(cls, settings: Settings) -> TemporalWorkflowStarter:
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        return cls(client, settings.temporal_task_queue)

    async def start_episode_workflow(self, *, episode_id: str) -> str:
        workflow_id = f"episode-{episode_id}"
        # workflow定義そのものはworkerが持つ。APIは名前と引数だけを知る（INV-1 / INV-3）。
        await self._client.start_workflow(
            "EpisodeSkeletonWorkflow",
            {"episode_id": episode_id},
            id=workflow_id,
            task_queue=self._task_queue,
        )
        return workflow_id
