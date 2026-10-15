"""``AgenticEpisodeWorkflow`` / ``AgenticLearningWorkflow`` の起動（Temporal の adapter）。

``application.ports.AgenticWorkflowStarter`` を構造的に満たす。workflow は名前と task queue で呼ぶ
（実装を import しない / INV-3）。Episode 1 つにつき ``AgenticEpisodeWorkflow`` は1度だけ
（REJECT_DUPLICATE: 終わった id も再利用しない）。
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from temporalio.api.enums.v1 import TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.agentic_pipeline import (
    AGENTIC_LEARNING_WORKFLOW,
    AGENTS_TASK_QUEUE,
    AgenticLearningInput,
    agentic_episode_workflow_id,
    agentic_learning_workflow_id,
)
from contracts.application import StartedWorkflow, WorkerCheck
from contracts.pipeline import PipelineOptions
from infrastructure.temporal.pipeline_options import agentic_episode_start_spec

#: poller の問い合わせを待つ上限（起動 API を遅くしない）
PROBE_TIMEOUT_SECONDS = 3.0


class TemporalAgenticStarter:
    def __init__(self, client: Client, options: PipelineOptions | None = None) -> None:
        self._client = client
        self._options = options if options is not None else PipelineOptions()

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        spec = agentic_episode_start_spec(
            episode_id=str(episode_id),
            template_id=template_id,
            template_version=template_version,
            options=self._options,
        )
        workflow_id = agentic_episode_workflow_id(str(episode_id))
        try:
            await self._client.start_workflow(
                spec.workflow_name,
                spec.input,
                id=workflow_id,
                task_queue=spec.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return StartedWorkflow(
                workflow_id=workflow_id, started=False, worker_check=await self.probe_workers()
            )
        return StartedWorkflow(
            workflow_id=workflow_id, started=True, worker_check=await self.probe_workers()
        )

    async def probe_workers(self) -> WorkerCheck:
        """workflow の queue（pipeline）と agent の activity queue に poller が居るか。

        ``pollers_present`` でも実行は断言しない（poller は workflow 型を名乗らない）。どちらかに
        poller が無いと確認できれば ``no_pollers``。確認できなければ（例外・応答なし）``unknown``。
        決して例外を投げない（起動の成否を変えない）。
        """
        checks = (
            (self._options.pipeline_task_queue, TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
            (AGENTS_TASK_QUEUE, TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
        )
        counts = [await self._pollers(name, kind) for name, kind in checks]
        if any(count == 0 for count in counts):
            return WorkerCheck.NO_POLLERS
        if any(count is None for count in counts):
            return WorkerCheck.UNKNOWN
        return WorkerCheck.POLLERS_PRESENT

    async def _pollers(self, name: str, kind: Any) -> int | None:
        try:
            response = await asyncio.wait_for(
                self._client.workflow_service.describe_task_queue(
                    DescribeTaskQueueRequest(
                        namespace=self._client.namespace,
                        task_queue=TaskQueue(name=name),
                        task_queue_type=kind,
                    )
                ),
                timeout=PROBE_TIMEOUT_SECONDS,
            )
            return len(response.pollers)
        except Exception:  # noqa: BLE001 - 確認できないことは unknown。起動を妨げない
            return None

    async def start_learning(self, episode_id: uuid.UUID) -> StartedWorkflow:
        """Learning は測定が揃うまで何度でも起動できる（走っている間は二重にしない）。"""
        name, queue = AGENTIC_LEARNING_WORKFLOW
        workflow_id = agentic_learning_workflow_id(str(episode_id))
        try:
            await self._client.start_workflow(
                name,
                AgenticLearningInput(episode_id=str(episode_id)),
                id=workflow_id,
                task_queue=queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return StartedWorkflow(workflow_id=workflow_id, started=False)
        return StartedWorkflow(workflow_id=workflow_id, started=True)
