"""Research の Activity 群（ADR-0037 §8.5）。``ResearchExecutor`` の薄いラッパ。

Activity は入力から結果を作るだけで、順序・retry の回し方は決めない（INV-4）。順序と retry の上限は
``workers/research/workflows.ResearchWorkflow`` だけが持つ。ロジック（予約・予算・crash 回復・
成果物の確定）は ``infrastructure/research/executor.py`` にあり、Temporal 無しでテストできる。

- 引数・戻り値は ``contracts/research.py`` の dataclass（ADR-0029 の型注釈どおりの形）。
  検索結果・本文・成果物の本体は載せず、参照（id・sha256）と件数だけ
- ``research_execute`` は依頼全体を 1 回実行する。長くなりうるので、実行中は一定間隔で heartbeat を
  送る（worker が落ちたことを Temporal が heartbeat timeout で知る。成功済みの呼び出しは再実行が
  生データから読むので送り直さない）
- 例外はそのまま送出する。Temporal は型名で分類する
  （``RESEARCH_WORKER_NON_RETRYABLE_ERROR_TYPE_NAMES``）
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from temporalio import activity

from contracts.research import (
    RESEARCH_EXECUTE_ACTIVITY,
    RESEARCH_RECORD_FAILURE_ACTIVITY,
    ResearchArtifactPointer,
    ResearchExecuteRequest,
    ResearchRecordFailureRequest,
    ResearchWorkflowOutput,
)
from infrastructure.research.executor import ResearchExecution, ResearchExecutor

#: ``research_execute`` の heartbeat の間隔（workflow の heartbeat timeout より十分短く）
RESEARCH_HEARTBEAT_INTERVAL_SECONDS = 20.0

HeartbeatFn = Callable[..., None]


def _activity_heartbeat(*details: Any) -> None:
    """Activity の中なら heartbeat を送る。外（単体テスト）では何もしない。"""
    try:
        activity.info()
    except RuntimeError:
        return
    activity.heartbeat(*details)


def to_output(execution: ResearchExecution) -> ResearchWorkflowOutput:
    """実行器の結果を境界の形にする（参照と件数だけ）。"""
    usage = execution.usage
    return ResearchWorkflowOutput(
        request_id=execution.request_id,
        status=execution.status.value,
        stop_code=execution.stop_code,
        artifact_refs=[
            ResearchArtifactPointer(
                artifact_type=ref.artifact_type.value,
                artifact_id=ref.artifact_id,
                sha256=ref.sha256,
            )
            for ref in execution.artifact_refs
        ],
        searches=usage.searches,
        fetches=usage.fetches,
        assessments=usage.assessments,
        youtube_units=usage.youtube_units,
        cost_usd=str(usage.cost_usd),
    )


class ResearchActivities:
    def __init__(
        self,
        executor: ResearchExecutor,
        *,
        heartbeat: HeartbeatFn = _activity_heartbeat,
        heartbeat_interval_seconds: float = RESEARCH_HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._executor = executor
        self._heartbeat = heartbeat
        self._interval = heartbeat_interval_seconds

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.execute, self.record_failure]

    @activity.defn(name=RESEARCH_EXECUTE_ACTIVITY)
    async def execute(self, request: ResearchExecuteRequest) -> ResearchWorkflowOutput:
        execution = await self._beating(self._executor.execute(request.request_id))
        return to_output(execution)

    @activity.defn(name=RESEARCH_RECORD_FAILURE_ACTIVITY)
    async def record_failure(self, request: ResearchRecordFailureRequest) -> ResearchWorkflowOutput:
        execution = await self._executor.record_failure(
            request.request_id, error_type=request.error_type, summary=request.summary
        )
        return to_output(execution)

    async def _beating[T](self, work: Awaitable[T]) -> T:
        """実行の完了を待つ間、``heartbeat_interval_seconds`` ごとに heartbeat を送る。"""
        task = asyncio.ensure_future(work)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=self._interval)
                if done:
                    return task.result()
                self._heartbeat("executing")
        except asyncio.CancelledError:
            task.cancel()
            raise


__all__ = ["RESEARCH_HEARTBEAT_INTERVAL_SECONDS", "ResearchActivities", "to_output"]
