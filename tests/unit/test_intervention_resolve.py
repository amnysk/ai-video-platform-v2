"""``resolve_annotation``（注記の解決。ADR-0041 addendum、intervention.md §4/§7）。

append-only（INV-41）・再送は1行1 event（INV-42）・AI に追加権限なし・read model と
ReviewPacket の「未解決コメント」に反映される。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from contracts.intervention import (
    PAYLOAD_MODELS,
    REQUIRED_SCOPE,
    AnnotationKind,
    CommandStatus,
    CommandType,
    InterventionEvent,
    RejectReason,
    ResolvePayload,
    Scope,
)
from infrastructure.db.models import AnnotationRow
from infrastructure.intervention.commands import CommandForbiddenError, Principal
from infrastructure.intervention.read_model import EpisodeReadModel
from infrastructure.intervention.reviewer import run_reviewer
from infrastructure.intervention.store import InterventionStore
from tests.support.intervention import AI, HUMAN, apply, envelope, make_episode, receive
from tests.unit.test_intervention_reviewer import _checks, _episode


async def _comment(session_factory, episode_id: str, kind: str = "comment") -> str:
    received = await receive(
        session_factory,
        episode_id,
        envelope(
            CommandType.COMMENT, {"anchor": {"segment_id": "s1"}, "body": "直す", "kind": kind}
        ),
    )
    return str((received.command.result or {})["annotation_id"])


async def _events(session_factory, episode_id: str) -> list[InterventionEvent]:
    async with session_factory() as s:
        return [e.event_name for e in await InterventionStore(s).list_events(episode_id)]


def test_the_contract_declares_the_command_scope_and_a_strict_payload() -> None:
    assert REQUIRED_SCOPE[CommandType.RESOLVE_ANNOTATION] is Scope.COMMENT
    assert PAYLOAD_MODELS[CommandType.RESOLVE_ANNOTATION] is ResolvePayload
    with pytest.raises(ValidationError):
        ResolvePayload.model_validate({"annotation_id": "a", "resolution": "done"})
    with pytest.raises(ValidationError):
        ResolvePayload.model_validate({"annotation_id": "a", "resolution": "resolved", "extra": 1})
    with pytest.raises(ValidationError):
        ResolvePayload.model_validate(
            {"annotation_id": "a", "resolution": "wont_fix", "note": "x" * 2000}
        )


async def test_resolving_appends_a_resolution_row_and_one_event_and_keeps_the_original(
    session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session)
    target = await _comment(session_factory, episode_id)
    env = envelope(
        CommandType.RESOLVE_ANNOTATION,
        {"annotation_id": target, "resolution": "wont_fix", "note": "仕様どおり"},
    )
    received = await receive(session_factory, episode_id, env)
    assert received.command.status is CommandStatus.APPLIED
    async with session_factory() as s:
        notes = await InterventionStore(s).list_annotations(episode_id)
    original, resolution = notes
    assert original.annotation_id == target and original.kind is AnnotationKind.COMMENT
    assert original.body == "直す"  # 元の行は変わらない
    assert resolution.kind is AnnotationKind.RESOLUTION
    assert resolution.refers_to_annotation_id == target
    assert resolution.payload == {"resolution": "wont_fix"}
    assert resolution.body == "仕様どおり"
    assert (await _events(session_factory, episode_id)).count(
        InterventionEvent.REVIEW_RESOLVED
    ) == 1

    # INV-42: 再適用・再送は1行1 event のまま
    again = await apply(session_factory, artifact_store, env.command_id)
    assert again.replayed and again.status is CommandStatus.APPLIED
    resent = await receive(session_factory, episode_id, env)
    assert not resent.created
    async with session_factory() as s:
        rows = await s.scalar(select(func.count()).select_from(AnnotationRow))
    assert rows == 2
    assert (await _events(session_factory, episode_id)).count(
        InterventionEvent.REVIEW_RESOLVED
    ) == 1


async def test_a_second_resolution_of_the_same_annotation_is_rejected_invalid_state(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    target = await _comment(session_factory, episode_id, "review_finding")
    body = {"annotation_id": target, "resolution": "resolved"}
    first = await receive(
        session_factory, episode_id, envelope(CommandType.RESOLVE_ANNOTATION, body)
    )
    second = await receive(
        session_factory, episode_id, envelope(CommandType.RESOLVE_ANNOTATION, body)
    )
    assert first.command.status is CommandStatus.APPLIED
    assert second.command.status is CommandStatus.REJECTED
    assert second.command.reject_reason is RejectReason.INVALID_STATE
    assert (await _events(session_factory, episode_id)).count(
        InterventionEvent.REVIEW_RESOLVED
    ) == 1


async def test_only_comments_and_findings_of_this_episode_can_be_resolved(
    session, session_factory
) -> None:
    episode_id = await make_episode(session)
    other_episode = await make_episode(session)
    foreign = await _comment(session_factory, other_episode)
    constraint = await receive(
        session_factory,
        episode_id,
        envelope(
            CommandType.SET_USER_CONSTRAINT, {"rule": "keep_segment_text", "target_ids": ["s1"]}
        ),
    )
    constraint_id = str((constraint.command.result or {})["annotation_id"])
    for target, reason in (
        (foreign, RejectReason.NOT_FOUND),
        ("00000000-0000-0000-0000-000000000000", RejectReason.NOT_FOUND),
        (constraint_id, RejectReason.INVALID_PAYLOAD),
    ):
        received = await receive(
            session_factory,
            episode_id,
            envelope(
                CommandType.RESOLVE_ANNOTATION, {"annotation_id": target, "resolution": "resolved"}
            ),
        )
        assert received.command.status is CommandStatus.REJECTED
        assert received.command.reject_reason is reason, target
    assert InterventionEvent.REVIEW_RESOLVED not in await _events(session_factory, episode_id)


async def test_ai_may_resolve_with_comment_scope_only(session, session_factory) -> None:
    episode_id = await make_episode(session)
    target = await _comment(session_factory, episode_id)
    body = {"annotation_id": target, "resolution": "resolved"}
    no_scope = Principal(AI.actor, frozenset({Scope.READ}))
    with pytest.raises(CommandForbiddenError):
        await receive(
            session_factory, episode_id, envelope(CommandType.RESOLVE_ANNOTATION, body), no_scope
        )
    commenter = Principal(AI.actor, frozenset({Scope.COMMENT}))
    received = await receive(
        session_factory, episode_id, envelope(CommandType.RESOLVE_ANNOTATION, body), commenter
    )
    assert received.command.status is CommandStatus.APPLIED
    assert received.command.actor == AI.actor
    assert HUMAN.actor != AI.actor


async def test_a_resolved_comment_leaves_the_read_model_and_review_packet_open_lists(
    session_factory, artifact_store
) -> None:
    episode_id, script, _ = await _episode(session_factory, artifact_store)
    target = await _comment(session_factory, episode_id)
    assert _checks(await run_reviewer(session_factory, artifact_store, episode_id))[
        "open_comments"
    ] == ["advisory"]

    await receive(
        session_factory,
        episode_id,
        envelope(
            CommandType.RESOLVE_ANNOTATION, {"annotation_id": target, "resolution": "resolved"}
        ),
    )

    view = await EpisodeReadModel(session_factory, artifact_store).load(episode_id)
    assert view is not None
    by_id = {a.annotation_id: a for a in view.annotations}
    assert by_id[target].status == "resolved" and by_id[target].resolved_by is not None
    assert "open_comments" not in _checks(
        await run_reviewer(session_factory, artifact_store, episode_id)
    )
