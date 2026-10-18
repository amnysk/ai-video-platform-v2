"""``AgentToolEvidencePort`` の実装（ADR-0047 §7）。PostgreSQL と MinIO の結果だけを読む。

**何も書かない。** 本体は MinIO（``agent_tool_calls.result_object_key``）。読み戻した sha256 を行の
``result_sha256`` と照合し、合わなければ ``integrity_error``（黙って返さない）。
入力は検証済みの tool 入力（Agent 自身の要求）で、token・秘密らしい項目は ``[redacted]`` にする。
結果は外部由来の文字列を含みうる（``untrusted``）ので、そのまま返し、指示として扱わせない。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.agent_tools import TOOL_CALL_STARTED, ToolEnvelope
from contracts.application import OperationError, OperationErrorCode
from contracts.application_operations import (
    ImprovementTrace,
    ListToolCallsRequest,
    ToolCallDetail,
    ToolCallList,
    ToolCallSummary,
)
from infrastructure.application.improvement_trace import build_improvement_trace
from infrastructure.db.models import AgentToolCallRow, EpisodeRow
from infrastructure.intervention.read_model import AnnotationView, EpisodeReadModel, EpisodeView
from infrastructure.storage.artifact_store import ArtifactStore

REDACTED = "[redacted]"
_SECRET_KEY = re.compile(r"token|secret|password|passwd|authorization|api[_-]?key|credential", re.I)


def redact(value: Any) -> Any:
    """秘密らしいキーの値を伏せる（tool 入力は schema 検証済みだが、出力の最後の防波堤）。"""
    if isinstance(value, dict):
        return {
            k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k) else redact(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


def annotations_for(view: EpisodeView, target_ref: str) -> tuple[dict[str, Any], ...]:
    """``anchor.target_ref`` がこの対象の注記（人の指摘。INV-63）。解決済みも状態つきで残す。"""
    return tuple(
        a.model_dump(mode="json")
        for a in view.annotations
        if a.anchor.get("target_ref") == target_ref
    )


def annotated(view: EpisodeView) -> list[AnnotationView]:
    return [a for a in view.annotations if a.anchor.get("target_ref")]


def summarize(row: AgentToolCallRow, view: EpisodeView) -> ToolCallSummary:
    return ToolCallSummary(
        tool_call_id=row.tool_call_id,
        tool_ref=f"{row.tool_id}@{row.tool_version}",
        agent_ref=f"{row.agent_role}@{row.agent_spec_version}",
        agent_role=row.agent_role,
        agent_spec_version=row.agent_spec_version,
        agent_run_key=row.agent_run_key,
        seq=row.seq,
        status=row.status,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        evidence_refs=tuple(row.evidence_refs or ()),
        research_request_id=row.research_request_id,
        error_code=row.error_code,
        input=redact(dict(row.input)),
        annotations=annotations_for(view, f"tool_call:{row.tool_call_id}"),
    )


class StoreBackedToolEvidenceReader:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store_factory: Callable[[], ArtifactStore],
    ) -> None:
        self._session_factory = session_factory
        self._store_factory = store_factory

    async def _view(self, episode_id: uuid.UUID) -> EpisodeView:
        view = await EpisodeReadModel(self._session_factory, self._store_factory()).load(
            str(episode_id)
        )
        if view is None:
            raise OperationError(
                OperationErrorCode.EPISODE_NOT_FOUND, f"episode {episode_id} not found"
            )
        return view

    async def list_tool_calls(
        self, episode_id: uuid.UUID, request: ListToolCallsRequest
    ) -> ToolCallList:
        view = await self._view(episode_id)
        stmt = (
            select(AgentToolCallRow)
            .where(AgentToolCallRow.episode_id == episode_id)
            .order_by(AgentToolCallRow.started_at.desc(), AgentToolCallRow.tool_call_id.desc())
        )
        if request.agent_role is not None:
            stmt = stmt.where(AgentToolCallRow.agent_role == request.agent_role.value)
        if request.tool_id is not None:
            stmt = stmt.where(AgentToolCallRow.tool_id == request.tool_id.value)
        if request.before is not None and request.before_id is not None:
            stmt = stmt.where(
                or_(
                    AgentToolCallRow.started_at < request.before,
                    and_(
                        AgentToolCallRow.started_at == request.before,
                        AgentToolCallRow.tool_call_id < request.before_id,
                    ),
                )
            )
        async with self._session_factory() as session:
            rows = list((await session.scalars(stmt.limit(request.limit + 1))).all())
        shown = rows[: request.limit]
        more = len(rows) > request.limit
        return ToolCallList(
            episode_id=episode_id,
            calls=tuple(summarize(r, view) for r in shown),
            next_before=shown[-1].started_at if more else None,
            next_before_id=shown[-1].tool_call_id if more else None,
        )

    async def get_tool_call(self, episode_id: uuid.UUID, tool_call_id: str) -> ToolCallDetail:
        view = await self._view(episode_id)
        async with self._session_factory() as session:
            row = await session.get(AgentToolCallRow, tool_call_id)
        if row is None or row.episode_id != episode_id:
            raise OperationError(
                OperationErrorCode.TOOL_CALL_NOT_FOUND,
                f"tool call {tool_call_id} not found in episode {episode_id}",
            )
        summary = summarize(row, view)
        result: dict[str, Any] | None = None
        missing: str | None = None
        if row.result_object_key is None or row.status == TOOL_CALL_STARTED:
            missing = (
                "started_without_result: the call may or may not have reached the provider"
                if row.status == TOOL_CALL_STARTED
                else "no_result_stored"
            )
        else:
            result = await self._verified_result(row)
        return ToolCallDetail(
            **summary.model_dump(),
            episode_id=episode_id,
            result=result,
            result_sha256=row.result_sha256,
            integrity="no_result" if result is None else "verified",
            result_missing_reason=missing,
        )

    async def _verified_result(self, row: AgentToolCallRow) -> dict[str, Any]:
        assert row.result_object_key is not None
        store = self._store_factory()
        detail = {"tool_call_id": row.tool_call_id, "expected_sha256": row.result_sha256}
        if not await store.exists(row.result_object_key):
            raise OperationError(
                OperationErrorCode.INTEGRITY_ERROR,
                "the stored result of this tool call is missing",
                {**detail, "reason": "result_object_missing"},
            )
        actual = await store.sha256_of(row.result_object_key)
        if actual != row.result_sha256:
            raise OperationError(
                OperationErrorCode.INTEGRITY_ERROR,
                "the stored result does not match its recorded sha256",
                {**detail, "actual_sha256": actual, "reason": "sha256_mismatch"},
            )
        envelope = ToolEnvelope.model_validate(await store.get_json(row.result_object_key))
        return envelope.model_dump(mode="json")

    async def improvement_trace(self, episode_id: uuid.UUID) -> ImprovementTrace:
        view = await self._view(episode_id)
        async with self._session_factory() as session:
            episode = await session.get(EpisodeRow, episode_id)
            calls = list(
                (
                    await session.scalars(
                        select(AgentToolCallRow)
                        .where(AgentToolCallRow.episode_id == episode_id)
                        .order_by(AgentToolCallRow.started_at, AgentToolCallRow.seq)
                    )
                ).all()
            )
        return await build_improvement_trace(
            episode_id=episode_id,
            topic=None if episode is None else episode.topic,
            view=view,
            tool_calls=[summarize(c, view) for c in calls],
            session_factory=self._session_factory,
            store=self._store_factory(),
        )
