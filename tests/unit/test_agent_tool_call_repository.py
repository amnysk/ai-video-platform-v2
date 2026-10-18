"""agent_tool_calls の永続化（ADR-0047 §6、INV-59）。SQLite + インメモリ ArtifactStore で走る。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from contracts.agent_tools import ToolEnvelope, ToolId, ToolStatus, input_sha256, tool_call_id
from domain.agents.tools.store import ToolCallCompletion, ToolCallStart
from infrastructure.db.agent_tool_call_repository import (
    AgentToolCallRepository,
    ToolCallIntegrityError,
    tool_call_object_key,
)
from infrastructure.db.models import AgentToolCallRow, EpisodeRow
from tests.support.agent_tools import EPISODE, RUN_KEY, SECRET_RESULT, FakeExecutor, make_gateway

NOW = datetime(2026, 10, 18, tzinfo=UTC)


async def _episode(session_factory) -> None:
    async with session_factory() as s:
        s.add(EpisodeRow(id=uuid.UUID(EPISODE), status="planned"))
        await s.commit()


def _start(seq: int = 1, q: str = "a") -> ToolCallStart:
    sha = input_sha256({"q": q})
    return ToolCallStart(
        tool_call_id=tool_call_id(RUN_KEY, seq, ToolId.SEARCH_TRENDS, sha),
        run_key=RUN_KEY,
        episode_id=EPISODE,
        agent_role="strategy",
        agent_spec_version="1.1",
        seq=seq,
        tool_id="search_trends",
        tool_version=1,
        input_sha256=sha,
        tool_input={"q": q},
        started_at=NOW,
    )


def _envelope(call_id: str, **kw: object) -> ToolEnvelope:
    base: dict[str, object] = {
        "tool_call_id": call_id,
        "tool_id": ToolId.SEARCH_TRENDS,
        "tool_version": 1,
        "status": ToolStatus.OK,
        "data": {"answer": SECRET_RESULT},
        "evidence_refs": ("research_request:r1",),
    }
    base.update(kw)
    return ToolEnvelope(**base)  # type: ignore[arg-type]


async def test_begin_commits_a_started_row_with_no_result(session_factory, artifact_store) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    start = _start()
    await repo.begin(start)
    stored = await repo.get(start.tool_call_id)
    assert stored is not None and stored.envelope is None and not stored.completed
    async with session_factory() as s:
        row = await s.get(AgentToolCallRow, start.tool_call_id)
    assert row is not None and row.status == "started"
    assert row.input == {"q": "a"} and row.result_object_key is None


async def test_begin_twice_with_the_same_id_is_a_no_op(session_factory, artifact_store) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    await repo.begin(_start())
    await repo.begin(_start())
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(AgentToolCallRow))).scalar() == 1


async def test_two_different_ids_cannot_take_the_same_run_and_seq(
    session_factory, artifact_store
) -> None:
    """handler が決定論でない（同じ順序で別の入力）なら握りつぶさず失敗する。"""
    from sqlalchemy.exc import IntegrityError

    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    await repo.begin(_start(seq=1, q="a"))
    with pytest.raises(IntegrityError):
        await repo.begin(_start(seq=1, q="b"))


async def test_complete_stores_the_body_in_the_object_store_and_only_a_reference_in_the_row(
    session_factory, artifact_store
) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    start = _start()
    await repo.begin(start)
    env = _envelope(start.tool_call_id)
    await repo.complete(
        start.tool_call_id,
        ToolCallCompletion(
            envelope=env,
            finished_at=NOW,
            duration_ms=12,
            research_request_id="req-1",
            error_code=None,
        ),
    )
    async with session_factory() as s:
        row = await s.get(AgentToolCallRow, start.tool_call_id)
    assert row is not None
    assert (row.status, row.duration_ms, row.research_request_id) == ("ok", 12, "req-1")
    assert row.result_object_key == tool_call_object_key(EPISODE, start.tool_call_id)
    assert row.evidence_refs == ["research_request:r1"]
    assert SECRET_RESULT not in repr(row.__dict__)  # 本文は行に無い
    assert row.result_sha256 == await artifact_store.sha256_of(row.result_object_key)
    stored = await repo.get(start.tool_call_id)
    assert stored is not None and stored.envelope == env and stored.completed


async def test_a_tampered_result_is_detected(session_factory, artifact_store) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    start = _start()
    await repo.begin(start)
    await repo.complete(
        start.tool_call_id,
        ToolCallCompletion(envelope=_envelope(start.tool_call_id), finished_at=NOW, duration_ms=1),
    )
    key = tool_call_object_key(EPISODE, start.tool_call_id)
    artifact_store._objects[key] = b'{"tampered": true}'
    with pytest.raises(ToolCallIntegrityError):
        await repo.get(start.tool_call_id)


async def test_completing_a_call_that_never_started_fails(session_factory, artifact_store) -> None:
    repo = AgentToolCallRepository(session_factory, artifact_store)
    with pytest.raises(ToolCallIntegrityError):
        await repo.complete(
            "00000000-0000-4000-8000-000000000000",
            ToolCallCompletion(
                envelope=_envelope("00000000-0000-4000-8000-000000000000"),
                finished_at=NOW,
                duration_ms=1,
            ),
        )


async def test_a_gateway_retry_on_the_real_repository_never_calls_the_executor_again(
    session_factory, artifact_store
) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    gw1, _, execs1 = make_gateway(store=repo)  # type: ignore[arg-type]
    first = await gw1.call_tool("search_trends@1", {"q": "a"})
    gw2, _, execs2 = make_gateway(store=repo)  # type: ignore[arg-type]
    again = await gw2.call_tool("search_trends@1", {"q": "a"})
    assert again == first
    assert len(execs1[ToolId.SEARCH_TRENDS].calls) == 1
    assert execs2[ToolId.SEARCH_TRENDS].calls == []
    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(AgentToolCallRow))).scalar() == 1


async def test_an_ambiguous_youtube_quota_row_on_the_real_repository_is_not_resent(
    session_factory, artifact_store
) -> None:
    await _episode(session_factory)
    repo = AgentToolCallRepository(session_factory, artifact_store)
    sha = input_sha256({"q": "v"})
    start = ToolCallStart(
        tool_call_id=tool_call_id(RUN_KEY, 1, ToolId.GET_VIDEO_ANALYTICS, sha),
        run_key=RUN_KEY,
        episode_id=EPISODE,
        agent_role="strategy",
        agent_spec_version="1.1",
        seq=1,
        tool_id="get_video_analytics",
        tool_version=1,
        input_sha256=sha,
        tool_input={"q": "v"},
        started_at=NOW,
    )
    await repo.begin(start)  # 前の試行: started のまま落ちた
    gw, _, execs = make_gateway(store=repo)  # type: ignore[arg-type]
    env = await gw.call_tool("get_video_analytics@1", {"q": "v"})
    assert env.status is ToolStatus.FAILED and "ambiguous_prior_attempt" in env.limitations
    assert execs[ToolId.GET_VIDEO_ANALYTICS].calls == []
    async with session_factory() as s:
        row = await s.get(AgentToolCallRow, start.tool_call_id)
    assert row is not None and row.status == "failed"
    assert row.error_code == "ambiguous_prior_attempt"
    assert isinstance(execs[ToolId.GET_VIDEO_ANALYTICS], FakeExecutor)
