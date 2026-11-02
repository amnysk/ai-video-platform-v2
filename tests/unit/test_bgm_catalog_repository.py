"""BGM カタログ・選曲の PostgreSQL 実装（SQLite で同じコードパスを走らせる。ADR-0087 §4）。

PostgreSQL 固有（JSONB・同時 INSERT）は tests/integration で固定する。ここは契約の振る舞い。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from contracts.music_selection import (
    DEFAULT_BRAND_POLICY,
    BgmSelectionDecision,
    FallbackEvent,
    FallbackStep,
)
from contracts.music_sources import (
    AcquisitionCandidateResult,
    AcquisitionOutcome,
    AcquisitionRunSummary,
    MusicSourceId,
)
from domain.music.auto_select import select_for_episode
from domain.music.requirements import derive_requirements
from infrastructure.db.bgm_repositories import SqlBgmCatalogRepository
from tests.support.bgm_auto import EPISODE, entry_for, loud_track, make_script, make_storyboard
from tests.support.bgm_catalog import NOW, make_track

SHA_A = "a" * 64


def _quarantined(track_id: str = "bgm-q1", sha_char: str = "a", **kw):
    return make_track(
        track_id,
        sha_char,
        status="quarantined",
        license_status="unverified",
        license_evidence_sha256=None,
        license_checked_at=None,
        loudness_lufs=-14.5,
        **kw,
    )


def _decision(episode: str = EPISODE, **update) -> BgmSelectionDecision:
    script = make_script()
    req = derive_requirements(
        script, make_storyboard(script), fmt="shorts", brand=DEFAULT_BRAND_POLICY
    )
    entries = [entry_for(loud_track("bgm-a", "a")), entry_for(loud_track("bgm-b", "b"))]
    base = select_for_episode(episode, req, entries, {}, NOW, brand=DEFAULT_BRAND_POLICY)
    return base.model_copy(update=update) if update else base


async def _register(repo: SqlBgmCatalogRepository, track):
    return await repo.register_quarantined(
        track,
        acquired_from="owner_supplied",
        acquired_at=NOW,
        evidence_object_key="music/evidence/x.json",
        analysis={"loudness_lufs": -14.5},
        inferred_tags={"mood": ["calm"]},
    )


# ------------------------------------------------------------------ catalog


async def test_a_quarantined_track_round_trips_with_every_field(session_factory) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    track = _quarantined(mix_gain_offset_db=6, allowed_brands=("default",), bpm=80)
    entry, created = await _register(repo, track)
    assert created and entry.track == track
    assert entry.analysis == {"loudness_lufs": -14.5}
    assert entry.inferred_tags == {"mood": ["calm"]}
    assert entry.acquired_at == NOW and entry.evidence_object_key == "music/evidence/x.json"
    assert (await repo.list_entries())[0].track == track
    assert (await repo.find_by_sha256(track.file_sha256)) == entry
    assert await repo.find_by_sha256("f" * 64) is None


async def test_the_same_content_is_not_registered_twice_and_the_first_row_wins(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    first, _ = await _register(repo, _quarantined("bgm-first"))
    second, created = await _register(repo, _quarantined("bgm-second"))
    assert not created and second.track.track_id == "bgm-first" == first.track.track_id
    assert len(await repo.list_entries()) == 1


async def test_a_track_must_be_registered_quarantined(session_factory) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    with pytest.raises(ValueError):
        await _register(repo, make_track("bgm-live", "b", loudness_lufs=-14.0))
    assert await repo.list_entries() == ()


async def test_promote_approves_only_a_quarantined_track_and_keeps_its_content(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    await _register(repo, _quarantined())
    approved = make_track(
        "bgm-q1",
        "a",
        loudness_lufs=-14.5,
        license_approver_kind="owner",
        mix_gain_offset_db=6,
    )
    entry = await repo.promote(approved, verified_at=NOW)
    assert entry.track.status == "active" and entry.track.license_status == "approved"
    assert entry.track.mix_gain_offset_db == 6 and entry.last_verified_at == NOW
    with pytest.raises(ValueError):  # 二度目は隔離ではない
        await repo.promote(approved, verified_at=NOW)
    with pytest.raises(LookupError):
        await repo.promote(make_track("bgm-none", "c"), verified_at=NOW)
    with pytest.raises(ValueError):  # 内容（sha256）は変えられない
        await repo.promote(make_track("bgm-q1", "b"), verified_at=NOW)
    with pytest.raises(ValueError):  # 承認でない曲では promote できない
        await repo.promote(_quarantined(), verified_at=NOW)


# ------------------------------------------------------------------ selections


async def test_save_selection_once_inserts_then_returns_the_existing_decision(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    first = _decision()
    assert await repo.save_selection_once(first, decided_at=NOW) == first
    other = first.model_copy(update={"track_id": "bgm-b"})
    again = await repo.save_selection_once(other, decided_at=NOW + timedelta(hours=1))
    assert again == first and again.track_id != "bgm-b"
    assert await repo.get_selection(EPISODE) == first
    assert await repo.get_selection(str(uuid.uuid4())) is None


async def test_concurrent_saves_leave_one_row_and_every_caller_sees_the_same_decision(
    tmp_path,
) -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from infrastructure.db.models import Base

    # 接続を共有しない実ファイルの SQLite（共有接続では並行の rollback が互いに干渉する）
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'race.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        repo = SqlBgmCatalogRepository(async_sessionmaker(engine, expire_on_commit=False))
        variants = [
            _decision().model_copy(update={"acquisition_requested": i % 2 == 0}) for i in range(6)
        ]
        seen = await asyncio.gather(
            *(repo.save_selection_once(v, decided_at=NOW) for v in variants)
        )
        assert len({d.model_dump_json() for d in seen}) == 1
        assert (await repo.get_selection(EPISODE)) == seen[0]
    finally:
        await engine.dispose()


async def test_finalize_sets_the_outcome_once_and_a_second_finalize_returns_the_first(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    saved = await repo.save_selection_once(_decision(), decided_at=NOW)
    assert not await repo.is_selection_finalized(EPISODE)
    final = saved.model_copy(
        update={
            "applied_gain_offset_db": 3,
            "min_speech_margin_centi_lu": 1420,
            "fallback": (
                *saved.fallback,
                FallbackEvent(step=FallbackStep.GAIN_REDUCED, track_id="bgm-a", gain_offset_db=-3),
            ),
        }
    )
    assert await repo.finalize_selection(final, finalized_at=NOW) == final
    assert await repo.is_selection_finalized(EPISODE)
    loser = final.model_copy(update={"track_id": None, "applied_gain_offset_db": 0})
    assert await repo.finalize_selection(loser, finalized_at=NOW) == final
    assert await repo.get_selection(EPISODE) == final
    # 確定後の save も既存を返す（再実行は保存した決定を使う）
    assert await repo.save_selection_once(_decision(), decided_at=NOW) == final


async def test_finalize_without_a_saved_row_inserts_a_finalized_one(session_factory) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    decision = _decision()
    assert await repo.finalize_selection(decision, finalized_at=NOW) == decision
    assert await repo.is_selection_finalized(EPISODE)


async def test_recent_usage_counts_the_last_window_of_finalized_selections_with_a_track(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)

    async def finalize(i: int, track_id: str | None) -> None:
        d = _decision(str(uuid.UUID(int=i + 1)), track_id=track_id)
        await repo.finalize_selection(d, finalized_at=NOW + timedelta(minutes=i))

    for i, track in enumerate(["bgm-a", "bgm-a", "bgm-b", None, "bgm-a"]):
        await finalize(i, track)
    # 確定していない選曲は数えない
    await repo.save_selection_once(_decision(str(uuid.UUID(int=99))), decided_at=NOW)
    assert await repo.recent_usage(window=10) == {"bgm-a": 3, "bgm-b": 1}
    # 直近 2 件（確定済み・曲あり）= bgm-a(i=4), bgm-b(i=2)。BGM 無しの i=3 は数えない
    assert await repo.recent_usage(window=2) == {"bgm-a": 1, "bgm-b": 1}
    assert await repo.recent_usage(window=0) == {}


# ------------------------------------------------------------------ acquisition


def _run(demand: str, finished: datetime) -> AcquisitionRunSummary:
    return AcquisitionRunSummary(
        demand_sha256=demand,
        started_at=finished - timedelta(minutes=1),
        finished_at=finished,
        searches=2,
        candidates=(
            AcquisitionCandidateResult(
                source=MusicSourceId.WIKIMEDIA_COMMONS,
                source_page_url="https://example.invalid/x",
                outcome=AcquisitionOutcome.DUPLICATE,
            ),
        ),
    )


async def test_demands_are_idempotent_pending_until_a_run_finishes_and_re_armed_after_a_day(
    session_factory,
) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    d1, d2 = "1" * 64, "2" * 64
    await repo.request_acquisition(d1, {"mood": "calm"}, requested_at=NOW)
    await repo.request_acquisition(d1, {"mood": "calm"}, requested_at=NOW + timedelta(minutes=5))
    await repo.request_acquisition(d2, {"mood": "tense"}, requested_at=NOW + timedelta(minutes=1))
    assert await repo.pending_demands(limit=10) == (
        (d1, {"mood": "calm"}),
        (d2, {"mood": "tense"}),
    )
    assert len(await repo.pending_demands(limit=1)) == 1

    assert await repo.last_acquisition_at(d1) is None
    await repo.record_acquisition_run(_run(d1, NOW + timedelta(hours=1)))
    assert await repo.last_acquisition_at(d1) == NOW + timedelta(hours=1)
    assert [d for d, _ in await repo.pending_demands(limit=10)] == [d2]

    # 実行済みの需要は、1 日以内の再要求では積み直さない。1 日経てば再び保留に出る
    await repo.request_acquisition(d1, {"mood": "calm"}, requested_at=NOW + timedelta(hours=2))
    assert [d for d, _ in await repo.pending_demands(limit=10)] == [d2]
    await repo.request_acquisition(d1, {"mood": "calm"}, requested_at=NOW + timedelta(days=2))
    assert [d for d, _ in await repo.pending_demands(limit=10)] == [d2, d1]


async def test_datetimes_must_be_timezone_aware(session_factory) -> None:
    repo = SqlBgmCatalogRepository(session_factory)
    with pytest.raises(ValueError):
        await repo.request_acquisition("1" * 64, {}, requested_at=datetime(2026, 11, 1))
