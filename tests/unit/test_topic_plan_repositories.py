"""TopicPlan / Content Memory / Analytics snapshot のリポジトリ（ADR-0025）。SQLite。"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select, update

from contracts.operations import ClaimOutcome
from contracts.states import EpisodeStatus, ProviderCall
from contracts.topic_planning import AnalyticsMode, DuplicateLevel, TopicPlanStatus
from infrastructure.db.models import EpisodeRow, TopicCandidateRow, TopicPlanRow
from infrastructure.db.repositories import (
    AnalyticsSnapshotRepository,
    DailyEpisodeSlotRepository,
    EpisodeRepository,
    NewTopicCandidate,
    NewTopicPlan,
    ProviderReservationRepository,
    TopicPlanRepository,
)

DAY = date(2026, 9, 18)


def new_plan(
    *, day: date = DAY, topic: str = "Why samurai wore two swords", subject: str = "daisho"
) -> NewTopicPlan:
    return NewTopicPlan(
        plan_date=day,
        strategy_profile_id="us_young_history_v1",
        strategy_version="1",
        content_profile_id="shorts",
        content_profile_version="1",
        topic=topic,
        subject=subject,
        angle="reason",
        era="edo",
        theme="warriors_and_war",
        hook="Two swords, one rule",
        entities=["samurai", subject],
        score=0.7,
        score_breakdown={"novelty": 1.0},
        duplicate_score=0.1,
        duplicate_level=DuplicateLevel.NONE,
        analytics_mode=AnalyticsMode.NO_ANALYTICS,
        analytics_confidence=0.0,
        planner_version="topic-planner-1",
        prompt_version="topic-candidates-1",
    )


def candidates(subject: str = "daisho") -> list[NewTopicCandidate]:
    return [
        NewTopicCandidate(
            ordinal=0,
            round=1,
            payload={"topic": "dup", "subject": "ninja"},
            subject="ninja",
            angle="myth_vs_fact",
            duplicate_level=DuplicateLevel.EXACT,
            duplicate_score=1.0,
            duplicate_of="Ninja myths",
            rejected=True,
        ),
        NewTopicCandidate(
            ordinal=1,
            round=1,
            payload={"topic": "ok", "subject": subject},
            subject=subject,
            angle="reason",
            duplicate_level=DuplicateLevel.NONE,
            duplicate_score=0.1,
            score=0.7,
            score_breakdown={"novelty": 1.0},
            rejected=False,
        ),
    ]


async def _save(session, **kw):
    saved = await TopicPlanRepository(session).save_plan(
        new_plan(**kw), candidates(kw.get("subject", "daisho")), selected_ordinal=1
    )
    await session.commit()
    return saved


# --------------------------------------------------------------------------- plans


@pytest.mark.asyncio
async def test_find_returns_none_then_the_saved_plan(session) -> None:
    repo = TopicPlanRepository(session)
    assert await repo.find(DAY, "us_young_history_v1", "shorts") is None
    saved = await _save(session)
    assert saved.reused is False
    found = await repo.find(DAY, "us_young_history_v1", "shorts")
    assert found is not None and found.id == saved.plan.id
    assert found.status is TopicPlanStatus.PLANNED
    assert await repo.find(DAY, "us_young_history_v1", "long_form") is None


@pytest.mark.asyncio
async def test_save_plan_stores_candidates_and_selected(session) -> None:
    saved = await _save(session)
    repo = TopicPlanRepository(session)
    stored = await repo.list_candidates(saved.plan.id)
    assert [c.ordinal for c in stored] == [0, 1]
    assert stored[0].rejected and stored[0].duplicate_level is DuplicateLevel.EXACT
    assert saved.plan.selected_candidate_id == stored[1].id
    assert saved.plan.entities == ["samurai", "daisho"]


@pytest.mark.asyncio
async def test_save_plan_never_overwrites_existing(session) -> None:
    first = await _save(session)
    second = await _save(session, topic="Something else entirely", subject="other")
    assert second.reused is True
    assert second.plan.id == first.plan.id
    assert second.plan.topic == first.plan.topic
    count = await session.scalar(select(func.count()).select_from(TopicPlanRow))
    assert count == 1
    assert await session.scalar(select(func.count()).select_from(TopicCandidateRow)) == 2


@pytest.mark.asyncio
async def test_save_plan_rejects_unknown_selected_ordinal(session) -> None:
    with pytest.raises(ValueError):
        await TopicPlanRepository(session).save_plan(new_plan(), candidates(), selected_ordinal=9)


# ------------------------------------------------------------------------- memory


@pytest.mark.asyncio
async def test_memory_includes_plans_and_legacy_episodes_deduped(session) -> None:
    plan = (await _save(session)).plan
    unlinked = (
        await _save(session, day=date(2026, 9, 19), topic="The last ronin", subject="ronin")
    ).plan
    episodes = EpisodeRepository(session)
    linked = await episodes.create(topic=plan.topic, topic_plan_id=plan.id)
    await session.execute(
        update(EpisodeRow)
        .where(EpisodeRow.id == uuid.UUID(linked.id))
        .values(status=EpisodeStatus.IN_PROGRESS.value)
    )
    await episodes.create(topic="Legacy topic about geisha")
    cancelled = await episodes.create(topic="Cancelled topic")
    await session.execute(
        update(EpisodeRow)
        .where(EpisodeRow.id == uuid.UUID(cancelled.id))
        .values(status=EpisodeStatus.CANCELLED.value)
    )
    await episodes.create(topic=None)
    await session.commit()

    memory = await TopicPlanRepository(session).list_memory()
    by_topic = {m.topic: m for m in memory}
    assert len(memory) == 3
    assert by_topic[plan.topic].status == "in_progress"
    assert by_topic[plan.topic].subject == "daisho"
    assert by_topic[plan.topic].day == "2026-09-18"
    assert by_topic[unlinked.topic].status == "planned"
    legacy = by_topic["Legacy topic about geisha"]
    assert legacy.subject is None and legacy.status == "planned"
    assert "Cancelled topic" not in by_topic


@pytest.mark.asyncio
async def test_plans_by_video_id_joins_spent_upload(session) -> None:
    plan = (await _save(session)).plan
    episode = await EpisodeRepository(session).create(topic=plan.topic, topic_plan_id=plan.id)
    legacy = await EpisodeRepository(session).create(topic="legacy")
    reservations = ProviderReservationRepository(session)
    for ep, video in ((episode.id, "vid_planned"), (legacy.id, "vid_legacy")):
        r = await reservations.reserve(
            episode_id=ep,
            job_id=None,
            provider=ProviderCall.YOUTUBE_UPLOAD,
            idempotency_key=uuid.uuid4().hex,
            input_hash="h" * 64,
            round=1,
        )
        await reservations.record_upload_result(r.id, video, reconciled_by="test")
    await session.commit()
    mapping = await TopicPlanRepository(session).plans_by_video_id(
        ["vid_planned", "vid_legacy", "unknown"]
    )
    assert set(mapping) == {"vid_planned"}
    assert mapping["vid_planned"].id == plan.id
    assert await TopicPlanRepository(session).plans_by_video_id([]) == {}


# ------------------------------------------------------------------------ analytics


@pytest.mark.asyncio
async def test_analytics_snapshot_idempotent_and_latest(session) -> None:
    repo = AnalyticsSnapshotRepository(session)
    assert await repo.latest("youtube_analytics") is None
    a = await repo.save(date(2026, 9, 17), "youtube_analytics", {"v": 1})
    again = await repo.save(date(2026, 9, 17), "youtube_analytics", {"v": 2})
    assert again.id == a.id and again.payload == {"v": 1}
    b = await repo.save(date(2026, 9, 18), "youtube_analytics", {"v": 3})
    await repo.save(date(2026, 9, 19), "other", {"v": 4})
    await session.commit()
    latest = await repo.latest("youtube_analytics")
    assert latest is not None and latest.id == b.id and latest.payload == {"v": 3}


# ------------------------------------------------------------------------ claim


async def _claim(
    session, trigger: str, plan_id: str | None, limit: int = 1, topic: str | None = "T"
):
    claim = await DailyEpisodeSlotRepository(session).claim(
        slot_date=DAY, trigger_id=trigger, daily_limit=limit, topic=topic, topic_plan_id=plan_id
    )
    await session.commit()
    return claim


@pytest.mark.asyncio
async def test_claim_created_links_plan_and_marks_assigned(session) -> None:
    plan = (await _save(session)).plan
    claim = await _claim(session, "t1", plan.id, topic=plan.topic)
    assert claim.outcome is ClaimOutcome.CREATED
    episode = await EpisodeRepository(session).get(claim.episode_id or "")
    assert episode is not None and episode.topic_plan_id == plan.id
    assert episode.topic == plan.topic
    refreshed = await TopicPlanRepository(session).get(plan.id)
    assert refreshed is not None and refreshed.status is TopicPlanStatus.ASSIGNED


@pytest.mark.asyncio
async def test_claim_resume_attaches_plan_to_unplanned_episode(session) -> None:
    legacy = await _claim(session, "crashed", None, topic=None)
    plan = (await _save(session)).plan
    resumed = await _claim(session, "next", plan.id, topic=plan.topic)
    assert resumed.outcome is ClaimOutcome.RESUME
    assert resumed.episode_id == legacy.episode_id
    episode = await EpisodeRepository(session).get(resumed.episode_id or "")
    assert episode is not None and episode.topic_plan_id == plan.id
    assert episode.topic == plan.topic
    refreshed = await TopicPlanRepository(session).get(plan.id)
    assert refreshed is not None and refreshed.status is TopicPlanStatus.ASSIGNED


@pytest.mark.asyncio
async def test_claim_plan_already_on_another_episode_is_limit_reached(session) -> None:
    plan = (await _save(session)).plan
    first = await _claim(session, "t1", plan.id, limit=2)
    await session.execute(
        update(EpisodeRow)
        .where(EpisodeRow.id == uuid.UUID(first.episode_id or ""))
        .values(status=EpisodeStatus.IN_PROGRESS.value)
    )
    await session.commit()
    # 枠は残っているが、同じ plan で2本目の Episode は作らない
    second = await _claim(session, "t2", plan.id, limit=2)
    assert second.outcome is ClaimOutcome.LIMIT_REACHED
    assert await session.scalar(select(func.count()).select_from(EpisodeRow)) == 1


@pytest.mark.asyncio
async def test_claim_plan_on_planned_episode_same_day_resumes_it(session) -> None:
    plan = (await _save(session)).plan
    first = await _claim(session, "t1", plan.id, limit=2)
    second = await _claim(session, "t2", plan.id, limit=2)
    assert second.outcome is ClaimOutcome.RESUME
    assert second.episode_id == first.episode_id
