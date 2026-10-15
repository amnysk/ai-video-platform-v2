"""``AgenticEpisodeWorkflow`` / ``AgenticLearningWorkflow`` の起動（Temporal の adapter）。

``application.ports.AgenticWorkflowStarter`` を構造的に満たす。workflow は名前と task queue で呼ぶ
（実装を import しない / INV-3）。Episode 1 つにつき ``AgenticEpisodeWorkflow`` は1度だけ
（REJECT_DUPLICATE: 終わった id も再利用しない）。
"""

from __future__ import annotations

import uuid

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.agentic_pipeline import (
    AGENTIC_EPISODE_WORKFLOW,
    AGENTIC_LEARNING_WORKFLOW,
    AgenticEpisodeInput,
    AgenticLearningInput,
    agentic_episode_workflow_id,
    agentic_learning_workflow_id,
)
from contracts.application import StartedWorkflow
from contracts.pipeline import PipelineOptions


class TemporalAgenticStarter:
    def __init__(self, client: Client, options: PipelineOptions | None = None) -> None:
        self._client = client
        self._options = options if options is not None else PipelineOptions()

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        name, queue = AGENTIC_EPISODE_WORKFLOW
        workflow_id = agentic_episode_workflow_id(str(episode_id))
        try:
            await self._client.start_workflow(
                name,
                AgenticEpisodeInput(
                    episode_id=str(episode_id),
                    template_id=template_id,
                    template_version=template_version,
                    options=self._options,
                ),
                id=workflow_id,
                task_queue=queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            return StartedWorkflow(workflow_id=workflow_id, started=False)
        return StartedWorkflow(workflow_id=workflow_id, started=True)

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
