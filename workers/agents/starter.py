"""内部（Agent）の ``SessionCommandPort`` が使う command workflow の starter。

``InterventionCommandWorkflow`` を決定的 id で start し、配送済みを記録する。
完了は待たない（INV-16）。同じ id が走っていれば Temporal が拒否する（= 既に配送済み）。
``apps/api`` の同名の starter と同じ動きで、worker から ``apps`` を import しないために別に持つ。
"""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.exceptions import WorkflowAlreadyStartedError

from contracts.intervention import INTERVENTION_COMMAND_WORKFLOW, intervention_command_workflow_id
from infrastructure.intervention.store import InterventionStore


class TemporalCommandStarter:
    def __init__(self, client: Any, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._client = client
        self._session_factory = session_factory

    async def start(self, command_id: str) -> None:
        workflow_name, task_queue = INTERVENTION_COMMAND_WORKFLOW
        with contextlib.suppress(WorkflowAlreadyStartedError):
            await self._client.start_workflow(
                workflow_name,
                {"command_id": command_id},
                id=intervention_command_workflow_id(command_id),
                task_queue=task_queue,
            )
        async with self._session_factory() as session:
            await InterventionStore(session).mark_command_dispatched(command_id)
            await session.commit()
