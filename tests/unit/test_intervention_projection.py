"""判断・介入イベントのログ投影と再投影（ADR-0041 §7、設計 §14/§15、INV-38/39）。

正本は ``episode_events``（PostgreSQL）。ログは commit 後の投影で、DB から決定的な ``event_id`` で
再投影できる。SQLite で実行する。理由は docs/testing/intervention-d-rationale.md §2。
"""

from __future__ import annotations

import logging
import re

import pytest

from contracts.intervention import ActorType, InterventionEvent
from contracts.log_contract import EventName
from infrastructure.db.repositories import EpisodeRepository
from infrastructure.intervention import projection
from infrastructure.intervention.store import Actor, InterventionStore
from infrastructure.logging.emit import emit
from infrastructure.logging.formatter import PROJECTED_EVENT_ID_KEY
from tests.support.log_capture import capture_json

HUMAN = Actor(ActorType.HUMAN, "owner")


async def _episode(session) -> str:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    return episode.id


def test_every_intervention_event_has_an_explicit_log_event() -> None:
    """対応表が網羅的（新しい InterventionEvent を足して投影を忘れない）。"""
    assert set(projection.EVENT_NAMES) == set(InterventionEvent)
    for source, target in projection.EVENT_NAMES.items():
        assert source.value == target.value


def test_event_id_is_deterministic_per_episode_and_seq() -> None:
    a = projection.projection_event_id("e1", 1)
    assert a == projection.projection_event_id("e1", 1)
    assert re.fullmatch(r"[0-9a-f]{32}", a)
    ids = {projection.projection_event_id(f"e{i}", s) for i in range(30) for s in range(1, 30)}
    assert len(ids) == 30 * 29


def test_formatter_uses_a_supplied_projection_event_id_only_when_well_formed() -> None:
    log = logging.getLogger("t.proj")
    good = "ab" * 16
    with capture_json(logging.INFO) as logs:
        emit(log, EventName.COMMENT_ADDED, logging.INFO, "x", **{PROJECTED_EVENT_ID_KEY: good})
        emit(log, EventName.COMMENT_ADDED, logging.INFO, "y", **{PROJECTED_EVENT_ID_KEY: "../x"})
    first, second = logs.events("intervention.comment.added")
    assert first["event_id"] == good
    assert second["event_id"] != "../x" and re.fullmatch(r"[0-9a-f]{32}", second["event_id"])
    assert PROJECTED_EVENT_ID_KEY not in first and PROJECTED_EVENT_ID_KEY not in first.get(
        "attributes", {}
    )


async def test_append_event_is_logged_only_after_commit(session) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    with capture_json(logging.INFO) as logs:
        await store.append_event(
            episode_id,
            InterventionEvent.HOLD_REQUESTED,
            HUMAN,
            {"manifest_id": "m-1", "control_revision": 3},
            command_id="cmd-00000001",
        )
        assert logs.events("intervention.hold.requested") == []  # commit 前は出ない
        await session.commit()
    [line] = logs.events("intervention.hold.requested")
    assert line["episode_id"] == episode_id
    assert line["event_seq"] == 1
    assert line["command_id"] == "cmd-00000001"
    assert line["actor_type"] == "human"
    assert line["manifest_id"] == "m-1"
    assert line["event_id"] == projection.projection_event_id(episode_id, 1)
    assert line["attributes"]["actor_id"] == "owner"


async def test_rolled_back_event_is_never_logged(session) -> None:
    episode_id = await _episode(session)
    with capture_json(logging.INFO) as logs:
        await InterventionStore(session).append_event(
            episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {}
        )
        await session.rollback()
        await session.commit()
    assert logs.events("intervention.comment.added") == []


async def test_only_reference_like_refs_reach_the_log(session) -> None:
    """INV-39: 本文・prompt を refs に入れられても、ログには ID・状態語だけが出る。"""
    episode_id = await _episode(session)
    secret_text = "please write the narration like this: SECRET-PROMPT-BODY"
    with capture_json(logging.INFO) as logs:
        await InterventionStore(session).append_event(
            episode_id,
            InterventionEvent.COMMENT_ADDED,
            HUMAN,
            {
                "annotation_id": "a-1",
                "body": secret_text,
                "prompt": secret_text,
                "kind": "comment",
                "nested": {"x": secret_text},
                "revision_ids": ["r1", "r2"],
            },
        )
        await session.commit()
    [line] = logs.events("intervention.comment.added")
    raw = logs.stream.getvalue()
    assert "SECRET-PROMPT-BODY" not in raw
    refs = line["attributes"]["refs"]
    assert refs == {"annotation_id": "a-1", "kind": "comment", "revision_ids": ["r1", "r2"]}
    assert sorted(line["attributes"]["refs_omitted"]) == ["body", "nested", "prompt"]


async def test_logging_failure_does_not_break_the_commit(session, monkeypatch) -> None:
    """INV-38: 投影の故障で正本の書き込みが失敗しない。"""

    def boom(*args, **kwargs):
        raise RuntimeError("logger broken")

    monkeypatch.setattr(projection, "event_fields", boom)
    episode_id = await _episode(session)
    store = InterventionStore(session)
    event = await store.append_event(episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {})
    await session.commit()
    assert [e.seq for e in await store.list_events(episode_id)] == [event.seq]


async def test_reproject_reemits_from_the_db_with_the_same_ids_and_marks_rows(
    session, session_factory
) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    with capture_json(logging.INFO) as live:
        for name in (InterventionEvent.COMMENT_ADDED, InterventionEvent.HOLD_REQUESTED):
            await store.append_event(episode_id, name, HUMAN, {})
        await session.commit()
    live_ids = [e["event_id"] for e in live.events() if e["event_name"] != "log.record"]

    with capture_json(logging.INFO) as again:
        result = await projection.reproject(session_factory, only_unprojected=True)
    assert result.emitted == 2
    assert [e["event_id"] for e in again.events() if e["event_name"] != "log.record"] == live_ids
    events = await InterventionStore(session).list_events(episode_id)
    assert all(e.projected_at is not None for e in events)

    with capture_json(logging.INFO) as third:
        result = await projection.reproject(session_factory, only_unprojected=True)
    assert result.emitted == 0
    assert third.names() == []


async def test_reproject_can_select_an_episode_and_a_seq_range(session, session_factory) -> None:
    episode_id = await _episode(session)
    other = await _episode(session)
    store = InterventionStore(session)
    for _ in range(4):
        await store.append_event(episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {})
    await store.append_event(other, InterventionEvent.COMMENT_ADDED, HUMAN, {})
    await session.commit()

    with capture_json(logging.INFO) as logs:
        result = await projection.reproject(
            session_factory, episode_id=episode_id, from_seq=2, to_seq=3, only_unprojected=False
        )
    assert result.emitted == 2
    assert [e["event_seq"] for e in logs.events("intervention.comment.added")] == [2, 3]
    assert {e["episode_id"] for e in logs.events("intervention.comment.added")} == {episode_id}


async def test_reproject_pages_through_more_rows_than_the_batch(session, session_factory) -> None:
    episode_id = await _episode(session)
    store = InterventionStore(session)
    for _ in range(7):
        await store.append_event(episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {})
    await session.commit()
    with capture_json(logging.INFO) as logs:
        result = await projection.reproject(session_factory, only_unprojected=True, batch_size=3)
    assert result.emitted == 7
    assert [e["event_seq"] for e in logs.events("intervention.comment.added")] == list(range(1, 8))


def test_reproject_rejects_a_seq_range_without_an_episode() -> None:
    with pytest.raises(ValueError):
        projection.validate_selection(episode_id=None, from_seq=1, to_seq=None)


# --------------------------------------------------------------------------- scripts/


def _script():
    import pathlib

    from tests.support.script_loader import load_script_module

    path = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "intervention-reproject.py"
    return load_script_module("intervention_reproject", path)


def test_script_refuses_a_seq_range_without_an_episode() -> None:
    with pytest.raises(SystemExit) as exc:
        _script().parse_args(["--from-seq", "2"])
    assert exc.value.code == 2


async def test_script_reprojects_unprojected_rows_then_nothing(session, session_factory) -> None:
    episode_id = await _episode(session)
    await InterventionStore(session).append_event(
        episode_id, InterventionEvent.COMMENT_ADDED, HUMAN, {}
    )
    await session.commit()
    script = _script()
    with capture_json(logging.INFO) as dry:
        assert await script.main(["--dry-run"], session_factory=session_factory) == 0
    assert dry.names() == []
    with capture_json(logging.INFO) as logs:
        assert await script.main([], session_factory=session_factory) == 0
    assert [e["event_seq"] for e in logs.events("intervention.comment.added")] == [1]
    with capture_json(logging.INFO) as logs:
        assert await script.main([], session_factory=session_factory) == 0
    assert logs.events("intervention.comment.added") == []
    with capture_json(logging.INFO) as logs:
        assert await script.main(["--all"], session_factory=session_factory) == 0
    assert len(logs.events("intervention.comment.added")) == 1
