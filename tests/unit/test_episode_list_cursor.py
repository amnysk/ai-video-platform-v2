"""list_episodes の複合 cursor と絞り込み（ADR-0045 §5）。

理由は docs/testing/jpmas2-p2b-rationale.md。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from contracts.application_operations import ListEpisodesRequest
from contracts.states import EpisodeStatus
from infrastructure.application.episode_reader import StoreBackedEpisodeReader
from infrastructure.db.models import EpisodeRow
from tests.support.intervention import make_episode

SAME = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def reader(session_factory, artifact_store) -> StoreBackedEpisodeReader:
    return StoreBackedEpisodeReader(session_factory, lambda: artifact_store)


async def seed(
    session_factory,
    *,
    topic: str | None = None,
    status: EpisodeStatus = EpisodeStatus.PLANNED,
    created_at: datetime = SAME,
) -> uuid.UUID:
    async with session_factory() as s:
        ep = uuid.UUID(await make_episode(s, status))
        row = await s.get(EpisodeRow, ep)
        assert row is not None
        row.created_at = created_at
        row.topic = topic
        await s.commit()
    return ep


async def test_same_timestamp_episodes_across_pages_lose_and_duplicate_nothing(
    reader, session_factory
) -> None:
    ids = {await seed(session_factory) for _ in range(5)}
    seen: list[uuid.UUID] = []
    before = before_id = None
    for _ in range(10):
        page = await reader.list_episodes(limit=2, before=before, before_id=before_id)
        seen += [e.episode_id for e in page.episodes]
        if page.next_before is None:
            assert page.next_before_id is None
            break
        assert page.next_before_id is not None
        before, before_id = page.next_before, page.next_before_id
    assert len(seen) == len(set(seen)) == 5
    assert set(seen) == ids


async def test_without_before_id_the_old_created_at_only_cursor_still_works(
    reader, session_factory
) -> None:
    older = await seed(session_factory, created_at=datetime(2025, 1, 1, tzinfo=UTC))
    await seed(session_factory)
    page = await reader.list_episodes(limit=10, before=SAME)
    assert [e.episode_id for e in page.episodes] == [older]


async def test_filter_by_status(reader, session_factory) -> None:
    await seed(session_factory, status=EpisodeStatus.PLANNED)
    other = await seed(session_factory, status=EpisodeStatus.SCRIPT_READY)
    page = await reader.list_episodes(limit=10, before=None, status=EpisodeStatus.SCRIPT_READY)
    assert [e.episode_id for e in page.episodes] == [other]


async def test_q_is_a_case_insensitive_substring_and_wildcards_are_literal(
    reader, session_factory
) -> None:
    hit = await seed(session_factory, topic="Cats and Dogs")
    pct = await seed(session_factory, topic="100% real")
    await seed(session_factory, topic="1000 real")
    await seed(session_factory, topic=None)
    got = await reader.list_episodes(limit=10, before=None, q="dOgS")
    assert [e.episode_id for e in got.episodes] == [hit]
    got = await reader.list_episodes(limit=10, before=None, q="100%")
    assert [e.episode_id for e in got.episodes] == [pct]
    got = await reader.list_episodes(limit=10, before=None, q="_")
    assert got.episodes == ()


def test_request_validates_q_length_and_defaults_are_backward_compatible() -> None:
    r = ListEpisodesRequest()
    assert r.before_id is None and r.status is None and r.q is None
    with pytest.raises(ValueError):
        ListEpisodesRequest(q="x" * 201)
    with pytest.raises(ValueError):
        ListEpisodesRequest(q="")
