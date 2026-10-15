"""介入 command の Activity（ADR-0041 §3）。本体は ``infrastructure.intervention.commands``。"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.exceptions import ApplicationError

from contracts.intervention import (
    APPLY_INTERVENTION_COMMAND_ACTIVITY,
    CHECK_PIPELINE_START_ACTIVITY,
    INTERVENTION_TASK_QUEUE,
    LIST_UNDELIVERED_COMMANDS_ACTIVITY,
    MARK_COMMAND_DISPATCHED_ACTIVITY,
    RECORD_COMMAND_FOLLOWUP_ACTIVITY,
    REDELIVERY_STALE_AFTER_SECONDS,
)
from contracts.pipeline import EpisodePipelineInput, PipelineOptions
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.commands import (
    CommandNotFoundError,
    CommandOutcome,
    apply_command,
    check_pipeline_start,
    record_followup,
)
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.artifact_store import ArtifactStore


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InterventionActivities:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        store: ArtifactStore,
        budget_policy: BudgetPolicy,
        pipeline_options: PipelineOptions,
        command_task_queue: str = INTERVENTION_TASK_QUEUE,
        clock: Callable[[], datetime] = _utcnow,
        redelivery_limit: int = 100,
    ) -> None:
        self._session_factory = session_factory
        self._store = store
        self._budget_policy = budget_policy
        self._pipeline_options = pipeline_options
        self._command_task_queue = command_task_queue
        self._clock = clock
        self._redelivery_limit = redelivery_limit

    def activities(self) -> Sequence[Callable[..., object]]:
        return [
            self.apply,
            self.check_start,
            self.followup,
            self.list_undelivered,
            self.mark_dispatched,
        ]

    def _response(self, outcome: CommandOutcome) -> dict[str, Any]:
        response: dict[str, Any] = {
            "command_id": outcome.command_id,
            "episode_id": outcome.episode_id,
            "type": outcome.type.value,
            "status": outcome.status.value,
            "reject_reason": None if outcome.reject_reason is None else outcome.reject_reason.value,
            "replayed": outcome.replayed,
            "pipeline": outcome.result.get("pipeline"),
        }
        start = outcome.pipeline_start
        if start is not None:
            pipeline_input = EpisodePipelineInput(
                episode_id=str(start["episode_id"]),
                options=self._pipeline_options,
                start_stage=str(start["start_stage"]),
                control_gate=bool(start.get("control_gate", False)),
            )
            response["pipeline_input"] = dataclasses.asdict(pipeline_input)
            response["pipeline_task_queue"] = self._pipeline_options.pipeline_task_queue
        return response

    @activity.defn(name=APPLY_INTERVENTION_COMMAND_ACTIVITY)
    async def apply(self, request: dict[str, Any]) -> dict[str, Any]:
        try:
            outcome = await apply_command(
                self._session_factory,
                self._store,
                str(request["command_id"]),
                budget_policy=self._budget_policy,
                clock=self._clock,
            )
        except CommandNotFoundError as exc:
            raise ApplicationError(
                f"command {exc} not found", type="CommandNotFoundError", non_retryable=True
            ) from None
        return self._response(outcome)

    @activity.defn(name=RECORD_COMMAND_FOLLOWUP_ACTIVITY)
    async def followup(self, request: dict[str, Any]) -> dict[str, Any]:
        outcome = await record_followup(
            self._session_factory, str(request["command_id"]), dict(request["pipeline"])
        )
        return self._response(outcome)

    @activity.defn(name=CHECK_PIPELINE_START_ACTIVITY)
    async def check_start(self, request: dict[str, Any]) -> dict[str, Any]:
        """子 pipeline を start してよいか。適用の後に状況が変わっていれば ``proceed=False``。"""
        try:
            return await check_pipeline_start(self._session_factory, str(request["command_id"]))
        except CommandNotFoundError as exc:
            raise ApplicationError(
                f"command {exc} not found", type="CommandNotFoundError", non_retryable=True
            ) from None

    @activity.defn(name=LIST_UNDELIVERED_COMMANDS_ACTIVITY)
    async def list_undelivered(self, request: dict[str, Any]) -> dict[str, Any]:
        """未配送・停滞した command を列挙する。配送済みの記録は start 成功後の別 Activity。"""
        stale_before = self._clock() - timedelta(seconds=REDELIVERY_STALE_AFTER_SECONDS)
        async with self._session_factory() as session:
            commands = await InterventionStore(session).list_undelivered_commands(
                stale_before=stale_before, limit=self._redelivery_limit
            )
        return {
            "command_ids": [c.command_id for c in commands],
            "task_queue": self._command_task_queue,
        }

    @activity.defn(name=MARK_COMMAND_DISPATCHED_ACTIVITY)
    async def mark_dispatched(self, request: dict[str, Any]) -> dict[str, Any]:
        """workflow の start に成功した（または既に走っている）command の配送を記録する。"""
        async with self._session_factory() as session:
            await InterventionStore(session).mark_command_dispatched(str(request["command_id"]))
            await session.commit()
        return {"command_id": str(request["command_id"])}
