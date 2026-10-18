"""人による根拠の指摘（ADR-0047 §7）: 既存の ``comment`` command が tool call・判断を指せる。

新しい command 型・ChangeKind は作らない。理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from contracts.intervention import (
    PAYLOAD_MODELS,
    Anchor,
    AnnotationFlag,
    AnnotationKind,
    CommandStatus,
    CommandType,
    CommentPayload,
    RejectReason,
)
from infrastructure.db.models import AgentToolCallRow, AnnotationRow, EpisodeEventRow
from infrastructure.intervention.store import Actor, InterventionStore
from tests.support.intervention import HUMAN, envelope, make_episode, receive

NOW = datetime(2026, 10, 18, tzinfo=UTC)
CALL_ID = str(uuid.uuid4())


async def _tool_call(session_factory, episode_id: str, call_id: str = CALL_ID) -> None:
    async with session_factory() as s:
        s.add(
            AgentToolCallRow(
                tool_call_id=call_id,
                agent_run_key="run-1",
                episode_id=uuid.UUID(episode_id),
                agent_role="strategy",
                agent_spec_version="1.1",
                seq=1,
                tool_id="search_trends",
                tool_version=1,
                input_sha256="a" * 64,
                input={"region": "JP"},
                status="ok",
                evidence_refs=[],
                started_at=NOW,
            )
        )
        await s.commit()


def _flag_payload(target: str, flag: str | None = "wrong_evidence", kind: str = "review_finding"):
    payload: dict = {"anchor": {"target_ref": target}, "body": "this source is wrong", "kind": kind}
    if flag is not None:
        payload["flag"] = flag
    return payload


def test_no_new_command_type_is_needed() -> None:
    """tool 呼び出しへの指摘は既存の comment の payload で表す（ADR-0047 §7）。"""
    assert PAYLOAD_MODELS[CommandType.COMMENT] is CommentPayload
    assert "flag_tool_evidence" not in {c.value for c in CommandType}


def test_target_ref_accepts_only_tool_call_or_decision_with_a_uuid() -> None:
    Anchor(target_ref=f"tool_call:{CALL_ID}")
    Anchor(target_ref=f"decision:{CALL_ID}")
    for bad in ("tool_call:abc", f"episode:{CALL_ID}", f"tool_call:{CALL_ID};drop", ""):
        with pytest.raises(ValidationError):
            Anchor(target_ref=bad)


def test_a_flag_needs_a_matching_target_and_a_review_finding() -> None:
    ok = CommentPayload.model_validate(_flag_payload(f"tool_call:{CALL_ID}"))
    assert ok.flag is AnnotationFlag.WRONG_EVIDENCE
    # flag が指す対象の種類と合わない
    with pytest.raises(ValidationError):
        CommentPayload.model_validate(_flag_payload(f"decision:{CALL_ID}", "wrong_evidence"))
    with pytest.raises(ValidationError):
        CommentPayload.model_validate(_flag_payload(f"tool_call:{CALL_ID}", "wrong_decision"))
    # target が無い flag
    with pytest.raises(ValidationError):
        CommentPayload.model_validate(
            {
                "anchor": {"segment_id": "s1"},
                "body": "x",
                "kind": "review_finding",
                "flag": "wrong_evidence",
            }
        )
    # 単なる comment は flag を持てない（flag は review_finding = 解決対象の指摘だけ）
    with pytest.raises(ValidationError):
        CommentPayload.model_validate(_flag_payload(f"tool_call:{CALL_ID}", kind="comment"))


async def test_a_human_can_flag_a_tool_call_as_wrong_evidence(session, session_factory) -> None:
    episode_id = await make_episode(session)
    await _tool_call(session_factory, episode_id)
    env = envelope(CommandType.COMMENT, _flag_payload(f"tool_call:{CALL_ID}"))
    received = await receive(session_factory, episode_id, env)
    assert received.command.status is CommandStatus.APPLIED
    async with session_factory() as s:
        (note,) = await InterventionStore(s).list_annotations(episode_id)
    assert note.kind is AnnotationKind.REVIEW_FINDING
    assert note.anchor == {"target_ref": f"tool_call:{CALL_ID}"}
    assert note.payload == {"flag": "wrong_evidence"}
    assert note.actor == Actor(HUMAN.actor.type, HUMAN.actor.id)


async def test_a_flag_on_an_unknown_or_foreign_tool_call_is_rejected_and_writes_nothing(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    other = await make_episode(session)
    await _tool_call(session_factory, other)  # 別 Episode の tool call
    for target in (f"tool_call:{CALL_ID}", f"decision:{uuid.uuid4()}"):
        flag = "wrong_evidence" if target.startswith("tool_call") else "wrong_decision"
        env = envelope(CommandType.COMMENT, _flag_payload(target, flag))
        received = await receive(session_factory, episode_id, env)
        assert received.command.status is CommandStatus.REJECTED
        assert received.command.reject_reason is RejectReason.NOT_FOUND
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AnnotationRow)) == 0


async def test_resending_the_same_flag_command_makes_one_logical_change(
    session, session_factory, artifact_store
) -> None:
    """INV-42: 同じ command_id の再送は注記も event も増やさない。"""
    from tests.support.intervention import apply

    episode_id = await make_episode(session)
    await _tool_call(session_factory, episode_id)
    env = envelope(CommandType.COMMENT, _flag_payload(f"tool_call:{CALL_ID}"))
    first = await receive(session_factory, episode_id, env)
    again = await receive(session_factory, episode_id, env)
    await apply(session_factory, artifact_store, env.command_id)
    assert first.created and not again.created
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(AnnotationRow)) == 1
        events = await s.scalar(select(func.count()).select_from(EpisodeEventRow))
    await receive(session_factory, episode_id, env)
    async with session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(EpisodeEventRow)) == events


async def test_the_same_command_id_with_a_different_target_is_refused(
    session, session_factory
) -> None:
    from infrastructure.intervention.commands import DuplicateCommandError

    episode_id = await make_episode(session)
    await _tool_call(session_factory, episode_id)
    first = envelope(CommandType.COMMENT, _flag_payload(f"tool_call:{CALL_ID}"))
    await receive(session_factory, episode_id, first)
    changed = first.model_copy(
        update={"payload": _flag_payload(f"tool_call:{CALL_ID}", "wrong_evidence") | {"body": "z"}}
    )
    with pytest.raises(DuplicateCommandError):
        await receive(session_factory, episode_id, changed)


async def test_a_human_can_send_back_an_agent_decision_with_a_flag(
    session, session_factory
) -> None:
    """判断の差し戻し = ``wrong_decision`` の指摘。承認の command は無い（docs: Open）。"""
    episode_id = await make_episode(session)
    async with session_factory() as s:
        decision = await InterventionStore(s).insert_decision(
            idempotency_key="decision:strategy:1",
            decision_type="choose_topic",
            actor=Actor(HUMAN.actor.type, "strategy"),
            payload={"decision_type": "choose_topic"},
            episode_id=episode_id,
        )
        await s.commit()
    env = envelope(
        CommandType.COMMENT,
        _flag_payload(f"decision:{decision.decision_id}", "wrong_decision"),
    )
    received = await receive(session_factory, episode_id, env)
    assert received.command.status is CommandStatus.APPLIED
    async with session_factory() as s:
        (note,) = await InterventionStore(s).list_annotations(episode_id)
    assert note.payload == {"flag": "wrong_decision"}
    assert note.anchor == {"target_ref": f"decision:{decision.decision_id}"}


def test_the_assistant_comment_tool_can_carry_the_flag_and_target() -> None:
    from apps.assistant.tools import build_command_payload
    from contracts.assistant_tools import TOOLS_BY_NAME

    args = {
        "episode_id": str(uuid.uuid4()),
        "anchor": {"target_ref": f"tool_call:{CALL_ID}"},
        "body": "wrong source",
        "kind": "review_finding",
        "flag": "wrong_evidence",
    }
    payload = build_command_payload("add_comment", args)
    assert CommentPayload.model_validate(payload).flag is AnnotationFlag.WRONG_EVIDENCE
    schema = TOOLS_BY_NAME["add_comment"].input_schema
    assert set(schema["properties"]["flag"]["enum"]) == {f.value for f in AnnotationFlag}
