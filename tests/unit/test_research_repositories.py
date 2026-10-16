"""Research の依頼と成果物の永続化（ADR-0037）。SQLite（tests/conftest.py）で走る。

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from contracts.research import (
    ResearchArtifactType,
    ResearchResult,
    ResearchStatus,
    parse_research_spec,
)
from domain.errors import InvalidTransitionError
from domain.research.errors import ResearchIdempotencyConflictError
from domain.research.identity import request_hash
from domain.research.ids import research_request_id_for
from domain.research.keys import research_artifact_object_key
from infrastructure.db.models import ResearchArtifactRow, ResearchRequestRow
from infrastructure.db.research_repositories import (
    ResearchArtifactRepository,
    ResearchRequestRepository,
)
from tests.support.research import evidence_payload, trend_payload

PCV = "provider-config-1"


async def _create(session, key: str = "evidence:ep-1", payload=None):
    spec = parse_research_spec(payload or evidence_payload())
    return await ResearchRequestRepository(session).create_or_get(
        idempotency_key=key, spec=spec, provider_config_version=PCV
    )


async def _record(session, request_id: str, sha: str = "a" * 64, *, kind=None):
    artifact_type = kind or ResearchArtifactType.RESEARCH_EVIDENCE
    return await ResearchArtifactRepository(session).record(
        request_id=request_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        bucket="artifacts",
        object_key=research_artifact_object_key(request_id, artifact_type, sha),
        sha256=sha,
        size_bytes=10,
    )


def _result(request_id: str, status: ResearchStatus, sha: str | None, artifact_id: str | None):
    refs = (
        [{"artifact_type": "research_evidence", "artifact_id": artifact_id, "sha256": sha}]
        if sha
        else []
    )
    return ResearchResult.model_validate(
        {
            "request_id": request_id,
            "execution_status": status.value,
            "artifact_refs": refs,
            "coverage": {"items_requested": 1, "items_covered": 1 if sha else 0},
        }
    )


# ------------------------------------------------------------------ 依頼


async def test_create_is_idempotent_by_key_and_derives_the_id(session) -> None:
    first = await _create(session)
    again = await _create(session)
    assert first.id == again.id == research_request_id_for("evidence:ep-1")
    assert first.status is ResearchStatus.QUEUED
    assert first.request_hash == request_hash(
        parse_research_spec(evidence_payload()), provider_config_version=PCV
    )
    count = await session.scalar(select(func.count()).select_from(ResearchRequestRow))
    assert count == 1


async def test_same_key_with_a_different_meaning_is_a_conflict_and_changes_nothing(
    session,
) -> None:
    first = await _create(session)
    other = evidence_payload(channel_id="channel-2")
    with pytest.raises(ResearchIdempotencyConflictError):
        await _create(session, payload=other)
    stored = await ResearchRequestRepository(session).get(first.id)
    assert stored is not None and stored.request_hash == first.request_hash


async def test_limits_and_payload_are_frozen_as_json(session) -> None:
    created = await _create(session, payload=trend_payload(limits={"max_cost_usd": "1.5"}))
    assert created.limits["max_cost_usd"] == "1.5"
    assert created.payload["kind"] == "trend"
    assert created.as_of.tzinfo is not None


async def test_status_transitions_go_through_the_table(session) -> None:
    repo = ResearchRequestRepository(session)
    created = await _create(session)
    with pytest.raises(InvalidTransitionError):
        await repo.finish(created.id, ResearchStatus.FAILED)
    running = await repo.mark_running(created.id)
    assert running.status is ResearchStatus.RUNNING and running.started_at is not None
    assert (await repo.mark_running(created.id)).status is ResearchStatus.RUNNING  # 再実行
    blocked = await repo.finish(
        created.id, ResearchStatus.BLOCKED, blocked_reason={"code": "provider_not_configured"}
    )
    assert blocked.blocked_reason == {"code": "provider_not_configured"}
    assert blocked.finished_at is None  # blocked は終端ではない
    resumed = await repo.resume(created.id)
    assert resumed.status is ResearchStatus.QUEUED and resumed.blocked_reason is None
    await repo.mark_running(created.id)
    failed = await repo.finish(created.id, ResearchStatus.FAILED)
    assert failed.finished_at is not None
    with pytest.raises(InvalidTransitionError):
        await repo.resume(created.id)


async def test_blocked_requires_a_reason_code(session) -> None:
    repo = ResearchRequestRepository(session)
    created = await _create(session)
    await repo.mark_running(created.id)
    with pytest.raises(ValueError):
        await repo.finish(created.id, ResearchStatus.BLOCKED)


async def test_completed_requires_its_artifact_to_be_recorded_first(session) -> None:
    """status だけ先に進んだ半端な状態を作らない（Artifact が先、completed が後）。"""
    repo = ResearchRequestRepository(session)
    created = await _create(session)
    await repo.mark_running(created.id)
    missing = _result(created.id, ResearchStatus.COMPLETED, "b" * 64, str(uuid.uuid4()))
    with pytest.raises(InvalidTransitionError):
        await repo.finish(created.id, ResearchStatus.COMPLETED, result=missing)
    with pytest.raises(ValueError):
        await repo.finish(created.id, ResearchStatus.COMPLETED)
    artifact = await _record(session, created.id, "b" * 64)
    ok = _result(created.id, ResearchStatus.COMPLETED, "b" * 64, artifact.id)
    done = await repo.finish(created.id, ResearchStatus.COMPLETED, result=ok)
    assert done.status is ResearchStatus.COMPLETED
    assert done.result_summary is not None
    assert done.result_summary["execution_status"] == "completed"


async def test_result_must_describe_the_same_request_and_status(session) -> None:
    repo = ResearchRequestRepository(session)
    created = await _create(session)
    await repo.mark_running(created.id)
    artifact = await _record(session, created.id)
    wrong_status = _result(created.id, ResearchStatus.PARTIAL, "a" * 64, artifact.id)
    with pytest.raises(ValueError):
        await repo.finish(created.id, ResearchStatus.COMPLETED, result=wrong_status)
    other_request = _result(str(uuid.uuid4()), ResearchStatus.COMPLETED, "a" * 64, artifact.id)
    with pytest.raises(ValueError):
        await repo.finish(created.id, ResearchStatus.COMPLETED, result=other_request)


async def _completed(session, key: str, sha: str = "a" * 64, payload=None):
    repo = ResearchRequestRepository(session)
    created = await _create(session, key, payload=payload)
    await repo.mark_running(created.id)
    artifact = await _record(session, created.id, sha)
    return await repo.finish(
        created.id,
        ResearchStatus.COMPLETED,
        result=_result(created.id, ResearchStatus.COMPLETED, sha, artifact.id),
    )


async def test_find_reusable_returns_a_fresh_completed_request_with_the_same_hash(
    session,
) -> None:
    repo = ResearchRequestRepository(session)
    done = await _completed(session, "evidence:ep-1")
    now = datetime.now(UTC)
    reused = await repo.find_reusable(done.request_hash, not_older_than=now - timedelta(days=30))
    assert reused is not None and reused.id == done.id
    assert await repo.find_reusable(done.request_hash, not_older_than=now + timedelta(1)) is None
    assert await repo.find_reusable("0" * 64, not_older_than=now - timedelta(days=30)) is None


async def test_find_reusable_skips_partial_and_running(session) -> None:
    repo = ResearchRequestRepository(session)
    long_ago = datetime.now(UTC) - timedelta(days=30)
    partial = await _create(session, "evidence:partial")
    await repo.mark_running(partial.id)
    artifact = await _record(session, partial.id)
    await repo.finish(
        partial.id,
        ResearchStatus.PARTIAL,
        result=_result(partial.id, ResearchStatus.PARTIAL, "a" * 64, artifact.id),
    )
    assert await repo.find_reusable(partial.request_hash, not_older_than=long_ago) is None
    running = await _create(session, "evidence:running")  # 同じ意味・別キー
    await repo.mark_running(running.id)
    await _record(session, running.id)
    assert running.request_hash == partial.request_hash
    assert await repo.find_reusable(partial.request_hash, not_older_than=long_ago) is None


async def test_list_unstarted_returns_old_queued_requests(session) -> None:
    repo = ResearchRequestRepository(session)
    queued = await _create(session, "evidence:q")
    started = await _create(session, "trend:s", payload=trend_payload())
    await repo.mark_running(started.id)
    future = datetime.now(UTC) + timedelta(minutes=1)
    assert [r.id for r in await repo.list_unstarted(older_than=future)] == [queued.id]
    assert await repo.list_unstarted(older_than=datetime.now(UTC) - timedelta(hours=1)) == []


# ------------------------------------------------------------------ 成果物


async def test_recording_a_new_artifact_supersedes_the_current_one(session) -> None:
    created = await _create(session)
    repo = ResearchArtifactRepository(session)
    first = await _record(session, created.id, "a" * 64)
    second = await _record(session, created.id, "b" * 64)
    assert (first.version, second.version) == (1, 2)
    current = await repo.find_current(created.id, ResearchArtifactType.RESEARCH_EVIDENCE)
    assert current is not None and current.id == second.id
    rows = (await session.scalars(select(ResearchArtifactRow))).all()
    assert sorted(r.superseded_at is None for r in rows) == [False, True]


async def test_recording_the_same_content_twice_is_idempotent(session) -> None:
    created = await _create(session)
    first = await _record(session, created.id, "a" * 64)
    again = await _record(session, created.id, "a" * 64)
    assert again.id == first.id
    count = await session.scalar(select(func.count()).select_from(ResearchArtifactRow))
    assert count == 1


async def test_a_to_b_to_a_restores_the_old_row_as_current(session) -> None:
    created = await _create(session)
    repo = ResearchArtifactRepository(session)
    first = await _record(session, created.id, "a" * 64)
    await _record(session, created.id, "b" * 64)
    back = await _record(session, created.id, "a" * 64)
    assert back.id == first.id
    current = await repo.find_current(created.id, ResearchArtifactType.RESEARCH_EVIDENCE)
    assert current is not None and current.sha256 == "a" * 64


async def test_artifact_types_do_not_supersede_each_other(session) -> None:
    created = await _create(session)
    for i, kind in enumerate(ResearchArtifactType):
        await _record(session, created.id, "abc"[i] * 64, kind=kind)
    current = await ResearchArtifactRepository(session).list_current(created.id)
    assert {a.artifact_type for a in current} == set(ResearchArtifactType)


async def test_artifact_key_must_be_the_research_key_of_that_request(session) -> None:
    """研究の成果物を Episode の artifacts/ 配下や別依頼のキーへ書かない。"""
    created = await _create(session)
    repo = ResearchArtifactRepository(session)
    other = str(uuid.uuid4())
    for key in (
        f"artifacts/{created.id}/research_evidence/{'a' * 64}.json",
        research_artifact_object_key(other, ResearchArtifactType.RESEARCH_EVIDENCE, "a" * 64),
        research_artifact_object_key(created.id, ResearchArtifactType.RESEARCH_EVIDENCE, "b" * 64),
    ):
        with pytest.raises(ValueError):
            await repo.record(
                request_id=created.id,
                artifact_type=ResearchArtifactType.RESEARCH_EVIDENCE,
                schema_version="1.0",
                bucket="artifacts",
                object_key=key,
                sha256="a" * 64,
                size_bytes=1,
            )


async def test_artifacts_need_an_existing_request(session) -> None:
    with pytest.raises(InvalidTransitionError):
        await _record(session, str(uuid.uuid4()))
