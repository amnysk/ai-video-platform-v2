"""入場トークンの workflow run が終わっているかを調べる port（ADR-0017 §8 / ADR-0019）。

``admit`` は ``in_progress`` のまま残った Episode を、記録された run が**閉じている**ときだけ
引き継ぐ。run が走っているなら決して引き継がない（二重実行になる）。
"""

from __future__ import annotations

from typing import Protocol

from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from domain.errors import TransientError


class WorkflowRunInspector(Protocol):
    async def is_closed(self, workflow_id: str, run_id: str) -> bool:
        """run が閉じていれば True。走っていれば False。問い合わせ失敗は ``TransientError``。"""
        ...


class TemporalWorkflowRunInspector:
    """Temporal の describe で run の状態を引く。"""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def is_closed(self, workflow_id: str, run_id: str) -> bool:
        try:
            description = await self._client.get_workflow_handle(
                workflow_id, run_id=run_id
            ).describe()
        except RPCError as exc:
            if exc.status is RPCStatusCode.NOT_FOUND:
                # 履歴の保持期間を過ぎた run。走っている run は必ず見つかる。
                return True
            raise TransientError(
                f"cannot describe workflow {workflow_id} run {run_id}: {exc.status.name}"
            ) from exc
        if description.status is None:
            raise TransientError(f"workflow {workflow_id} run {run_id} has no status yet")
        return description.status is not WorkflowExecutionStatus.RUNNING


__all__ = ["TemporalWorkflowRunInspector", "WorkflowRunInspector"]
