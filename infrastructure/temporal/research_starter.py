"""``ResearchWorkflow`` の起動（ADR-0037 §8.5）。API（受け付け・再開）が使う。

**完了は待たない**（INV-16）。

- workflow id は依頼 1 件につき 1 つ（``research_workflow_id``）。**実行中の同じ id は Temporal が
  構造的に拒否する**ので、同じ依頼を同時に 2 つ走らせない。拒否（``WorkflowAlreadyStartedError``）は
  「もう走っている」なので成功として扱い、同じ id を返す
- id の再利用は ``ALLOW_DUPLICATE``（既定）: 依頼を ``blocked`` で止めた workflow は
  Temporal の上では**成功終了**しているので、``ALLOW_DUPLICATE_FAILED_ONLY`` だと
  再開（``blocked → queued``）した依頼を二度と走らせられない。終了済みの id で再び走らせても、
  executor は終わった依頼を書き換えず、成功済みの呼び出しを送り直さない（冪等）
- 入力は依頼 ID だけ（中身は DB にある）
"""

from __future__ import annotations

import contextlib
from typing import Protocol

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.research import RESEARCH_WORKFLOW, ResearchWorkflowInput, research_workflow_id
from infrastructure.config import Settings

__all__ = ["ResearchWorkflowStarter", "TemporalResearchStarter", "start_research_workflow"]


class ResearchWorkflowStarter(Protocol):
    async def start_research(self, *, request_id: str) -> str:
        """``ResearchWorkflow`` を起動し、workflow id を返す。実行中なら何もせず同じ id を返す。"""
        ...


async def start_research_workflow(client: Client, *, request_id: str) -> str:
    workflow_name, task_queue = RESEARCH_WORKFLOW
    workflow_id = research_workflow_id(request_id)
    # 同じ依頼の workflow が実行中なら Temporal が拒否する（二重に走らせない）。それは成功として扱う
    with contextlib.suppress(WorkflowAlreadyStartedError):
        await client.start_workflow(
            workflow_name,
            ResearchWorkflowInput(request_id=request_id),
            id=workflow_id,
            task_queue=task_queue,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    return workflow_id


class TemporalResearchStarter:
    def __init__(self, client: Client) -> None:
        self._client = client

    @classmethod
    async def connect(cls, settings: Settings) -> TemporalResearchStarter:  # pragma: no cover
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        return cls(client)

    async def start_research(self, *, request_id: str) -> str:
        return await start_research_workflow(self._client, request_id=request_id)
