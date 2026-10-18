"""``ToolCallStore`` の実装（ADR-0047 §3 / §6、INV-59）。

- ``begin``: 実行前に ``started`` を**自分の transaction で commit** する
  （落ちても「呼ぶ予定だった」が残る）
- ``complete``: 結果の envelope を MinIO（``agent-runs/{episode}/tool-calls/{id}.json``）に置き、
  読み戻した sha256 を行に記録してから commit する。行に本文は持たない
- ``get``: 行があり結果があれば MinIO から読み戻して sha256 を照合した envelope を返す。
  ``started`` のままなら ``envelope=None``（呼んだか不明）

DB の ``input`` は検証済みの tool 入力。外部由来の結果本文は MinIO にだけ置く。
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agent_tools import TOOL_CALL_STARTED, ToolEnvelope
from domain.agents.tools.store import StoredToolCall, ToolCallCompletion, ToolCallStart
from infrastructure.db.models import AgentToolCallRow
from infrastructure.intervention.capture import AGENT_RUNS_PREFIX
from infrastructure.storage.artifact_store import ArtifactStore


class ToolCallIntegrityError(RuntimeError):
    """保存済みの結果が行の sha256 と合わない、または行が無いのに完了しようとした。"""


def tool_call_object_key(episode_id: str, tool_call_id: str) -> str:
    return f"{AGENT_RUNS_PREFIX}/{episode_id}/tool-calls/{tool_call_id}.json"


class AgentToolCallRepository:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
    ) -> None:
        self._sf = session_factory
        self._store = store

    async def get(self, tool_call_id: str) -> StoredToolCall | None:
        async with self._sf() as session:
            row = await session.get(AgentToolCallRow, tool_call_id)
        if row is None:
            return None
        if row.status == TOOL_CALL_STARTED or row.result_object_key is None:
            return StoredToolCall(
                tool_call_id=tool_call_id,
                envelope=None,
                research_request_id=row.research_request_id,
                error_code=row.error_code,
            )
        digest = await self._store.sha256_of(row.result_object_key)
        if digest != row.result_sha256:
            raise ToolCallIntegrityError(
                f"stored result of tool call {tool_call_id} does not match its recorded sha256"
            )
        envelope = ToolEnvelope.model_validate(await self._store.get_json(row.result_object_key))
        return StoredToolCall(
            tool_call_id=tool_call_id,
            envelope=envelope,
            research_request_id=row.research_request_id,
            error_code=row.error_code,
        )

    async def begin(self, start: ToolCallStart) -> None:
        async with self._sf() as session:
            session.add(
                AgentToolCallRow(
                    tool_call_id=start.tool_call_id,
                    agent_run_key=start.run_key,
                    episode_id=uuid.UUID(start.episode_id),
                    agent_role=start.agent_role,
                    agent_spec_version=start.agent_spec_version,
                    seq=start.seq,
                    tool_id=start.tool_id,
                    tool_version=start.tool_version,
                    input_sha256=start.input_sha256,
                    input=dict(start.tool_input),
                    status=TOOL_CALL_STARTED,
                    evidence_refs=[],
                    started_at=start.started_at,
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # 同じ tool_call_id が既にあるなら何もしない。別の ID が同じ (run, seq) を
                # 取っていたら
                # handler が決定論でない: 握りつぶさず伝える
                if await session.get(AgentToolCallRow, start.tool_call_id) is None:
                    raise

    async def complete(self, tool_call_id: str, completion: ToolCallCompletion) -> None:
        async with self._sf() as session:
            row = (
                await session.execute(
                    select(AgentToolCallRow).where(AgentToolCallRow.tool_call_id == tool_call_id)
                )
            ).scalar_one_or_none()
            if row is None:
                raise ToolCallIntegrityError(f"tool call {tool_call_id} was never started")
            key = tool_call_object_key(str(row.episode_id), tool_call_id)
            put = await self._store.put_json(key, completion.envelope.model_dump(mode="json"))
            row.status = completion.envelope.status.value
            row.result_object_key = key
            row.result_sha256 = put.sha256
            row.evidence_refs = list(completion.envelope.evidence_refs)
            row.research_request_id = completion.research_request_id
            row.finished_at = completion.finished_at
            row.duration_ms = completion.duration_ms
            row.error_code = completion.error_code
            await session.commit()
