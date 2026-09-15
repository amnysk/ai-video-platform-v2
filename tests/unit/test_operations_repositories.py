"""運用スイッチと日次 Episode 枠の永続化（ADR-0021）。

SQLite に対して実行する。並行 claim を実PostgreSQLで踏む版は
tests/integration/test_daily_slot_concurrency.py。
"""

from __future__ import annotations

from datetime import date

from contracts.operations import ClaimOutcome, OperationalSwitch

from contracts.states import EpisodeStatus
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.repositories import (
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    OperationalSwitchRepository,
)

TODAY = date(2026, 9, 15)
YESTERDAY = date(2026, 9, 14)


async def test_missing_switch_is_off(session) -> None:
    switches = OperationalSwitchRepository(session)
    assert await switches.is_on(OperationalSwitch.PAUSED) is False
    assert await switches.is_on(OperationalSwitch.UPLOADS_PAUSED) is False


async def test_switch_set_upserts_and_is_independent(session) -> None:
    switches = OperationalSwitchRepository(session)
    await switches.set(OperationalSwitch.PAUSED, True, reason="maintenance")
    await session.commit()
    assert await switches.is_on(OperationalSwitch.PAUSED) is True
    assert await switches.is_on(OperationalSwitch.UPLOADS_PAUSED) is False

    await switches.set(OperationalSwitch.PAUSED, False)
    await session.commit()
    assert await switches.is_on(OperationalSwitch.PAUSED) is False


async def _start(session, episode_id: str) -> None:
    await EpisodeRepository(session).apply_event(episode_id, EpisodeEvent.WORKFLOW_STARTED)
    await session.commit()


async def test_first_claim_creates_a_planned_episode(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    claim = await slots.claim(slot_date=TODAY, trigger_id="t1", daily_limit=1, topic="topic")
    await session.commit()

    assert claim.outcome is ClaimOutcome.CREATED
    assert claim.episode_id is not None
    assert claim.slot_index == 0
    episode = await EpisodeRepository(session).get(claim.episode_id)
    assert episode is not None and episode.status is EpisodeStatus.PLANNED
    assert episode.topic == "topic"
    rows = await slots.list_for_date(TODAY)
    assert [(r.slot_index, r.trigger_id, r.episode_id) for r in rows] == [
        (0, "t1", claim.episode_id)
    ]


async def test_retry_of_the_same_trigger_returns_the_existing_slot(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    first = await slots.claim(slot_date=TODAY, trigger_id="t1", daily_limit=1, topic=None)
    await session.commit()
    await _start(session, first.episode_id or "")

    again = await slots.claim(slot_date=TODAY, trigger_id="t1", daily_limit=1, topic=None)
    assert again.outcome is ClaimOutcome.EXISTING
    assert again.episode_id == first.episode_id
    assert len(await slots.list_for_date(TODAY)) == 1


async def test_second_trigger_on_the_same_day_hits_the_limit(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    first = await slots.claim(slot_date=TODAY, trigger_id="t1", daily_limit=1, topic=None)
    await session.commit()
    await _start(session, first.episode_id or "")

    second = await slots.claim(slot_date=TODAY, trigger_id="t2", daily_limit=1, topic=None)
    assert second.outcome is ClaimOutcome.LIMIT_REACHED
    assert second.episode_id is None
    assert len(await slots.list_for_date(TODAY)) == 1


async def test_limit_reached_resumes_a_planned_but_unstarted_episode(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    first = await slots.claim(slot_date=TODAY, trigger_id="t1", daily_limit=1, topic=None)
    await session.commit()  # 開始前に落ちた（planned のまま）

    second = await slots.claim(slot_date=TODAY, trigger_id="t2", daily_limit=1, topic=None)
    assert second.outcome is ClaimOutcome.RESUME
    assert second.episode_id == first.episode_id
    assert len(await slots.list_for_date(TODAY)) == 1


async def test_a_new_date_creates_a_new_slot(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    first = await slots.claim(slot_date=YESTERDAY, trigger_id="t1", daily_limit=1, topic=None)
    await session.commit()
    await _start(session, first.episode_id or "")

    today = await slots.claim(slot_date=TODAY, trigger_id="t2", daily_limit=1, topic=None)
    assert today.outcome is ClaimOutcome.CREATED
    assert today.episode_id != first.episode_id
    assert today.slot_index == 0


async def test_yesterdays_blocked_episode_does_not_affect_today(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    episodes = EpisodeRepository(session)
    first = await slots.claim(slot_date=YESTERDAY, trigger_id="t1", daily_limit=1, topic=None)
    await session.commit()
    await _start(session, first.episode_id or "")
    await episodes.apply_event(
        first.episode_id or "", EpisodeEvent.NEEDS_INPUT_FAILURE, blocked_reason="x"
    )
    await session.commit()

    today = await slots.claim(slot_date=TODAY, trigger_id="t2", daily_limit=1, topic=None)
    assert today.outcome is ClaimOutcome.CREATED
    blocked = await episodes.get(first.episode_id or "")
    assert blocked is not None and blocked.status is EpisodeStatus.BLOCKED


async def test_higher_limit_allows_several_slots_per_day(session) -> None:
    slots = DailyEpisodeSlotRepository(session)
    claims = []
    for trigger in ("t1", "t2", "t3"):
        claim = await slots.claim(slot_date=TODAY, trigger_id=trigger, daily_limit=2, topic=None)
        await session.commit()
        if claim.outcome is ClaimOutcome.CREATED:
            await _start(session, claim.episode_id or "")
        claims.append(claim)
    assert [c.outcome for c in claims] == [
        ClaimOutcome.CREATED,
        ClaimOutcome.CREATED,
        ClaimOutcome.LIMIT_REACHED,
    ]
    assert [c.slot_index for c in claims[:2]] == [0, 1]
