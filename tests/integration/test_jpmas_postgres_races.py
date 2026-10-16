"""JP-MAS-001 §17-4 と INV-43/44/46 の競合を、実 PostgreSQL の別接続で確かめる。

SQLite は ``FOR UPDATE`` / ``FOR SHARE`` を無視し、接続も 1 本（StaticPool）なので、行ロックでの
直列化は単体試験では言えない。ここは接続プールの別接続（別 transaction）を ``asyncio.gather`` で
同時に走らせ、競合の両側の結果を数える。理由は docs/testing/jpmas-integration-rationale.md。

スキーマは使い捨て（``*_test`` DB の一時スキーマ）、bucket は ``artifacts-test``。
有料 provider は呼ばない
（予約行を作って ``mark_dispatched`` を呼ぶだけで、送信はしない）。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.intervention import (
    ChangeSetStatus,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    RejectReason,
)
from contracts.states import ArtifactType, EpisodeStatus, ProviderCall
from domain.errors import EpisodeHeldError
from infrastructure.db.models import Base
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    ProviderReservationRepository,
)
from infrastructure.intervention.store import InterventionStore
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.intervention import (
    apply,
    envelope,
    make_episode,
    receive,
    seed_script_and_storyboard,
    submit,
)

TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="TEST_DATABASE_URL (*_test) and MINIO_ENDPOINT are required",
)

ROUNDS = 6


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"jpmas_race_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    # 別接続で本当に並行させる（プールを広げる）
    engine = create_async_engine(
        TEST_DATABASE_URL or "",
        connect_args={"options": f"-c search_path={schema}"},
        pool_size=10,
        max_overflow=10,
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from tests.support.minio import connect_test_artifact_store

    return await connect_test_artifact_store()


def _script_edit(base_id: str, narration: str) -> dict:
    return {
        "change": {
            "kind": "script.edit_scenes",
            "base_revision_id": base_id,
            "edits": [{"segment_id": "s2", "narration": narration}],
        },
        "rationale": "競合の試験",
    }


# --------------------------------------------------------------------------- §17-4


async def test_two_concurrent_applies_on_the_same_base_revision_give_one_applied_one_conflict(
    pg_session_factory, minio_store
) -> None:
    """§17-4 / INV-43: 同じ版を人が2つの案で同時に採用 → 片方 APPLIED、もう片方 CONFLICT。"""
    for round_no in range(ROUNDS):
        async with pg_session_factory() as s:
            episode_id = await make_episode(s, EpisodeStatus.ASSETS_READY)
            script, _storyboard, _media = await seed_script_and_storyboard(
                s, minio_store, episode_id
            )
        proposals = []
        for n in range(2):
            outcome = await submit(
                pg_session_factory,
                minio_store,
                episode_id,
                envelope(
                    CommandType.PROPOSE_CHANGE,
                    _script_edit(script.id, f"煮炊きの跡が残っている。案{round_no}-{n}"),
                ),
            )
            assert outcome.status is CommandStatus.APPLIED, outcome.result
            proposals.append(outcome.result["change_set_id"])
        applies = [envelope(CommandType.APPLY_CHANGE, {"change_set_id": cs}) for cs in proposals]
        for env in applies:
            await receive(pg_session_factory, episode_id, env)
        results = await asyncio.gather(
            *[apply(pg_session_factory, minio_store, env.command_id) for env in applies],
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        statuses = sorted(r.status.value for r in results)  # type: ignore[union-attr]
        assert statuses == [CommandStatus.APPLIED.value, CommandStatus.CONFLICT.value], statuses
        winner = next(r for r in results if r.status is CommandStatus.APPLIED)  # type: ignore[union-attr]
        loser = next(r for r in results if r.status is CommandStatus.CONFLICT)  # type: ignore[union-attr]
        assert loser.reject_reason is RejectReason.STALE_TARGET_REVISION  # type: ignore[union-attr]
        async with pg_session_factory() as s:
            current = await ArtifactMetadataRepository(s).find_current_by_type(
                episode_id, ArtifactType.SCRIPT
            )
            assert current is not None and current.id == winner.result["revision_id"]  # type: ignore[union-attr]
            store = InterventionStore(s)
            by_status = [
                (await store.get_change_set(cs)).status  # type: ignore[union-attr]
                for cs in proposals
            ]
            assert sorted(x.value for x in by_status) == sorted(
                [ChangeSetStatus.APPLIED.value, ChangeSetStatus.CONFLICTED.value]
            )
            events = [e.event_name for e in await store.list_events(episode_id)]
        assert events.count(InterventionEvent.CHANGE_APPLIED) == 1


async def test_a_human_and_an_ai_applying_the_same_base_concurrently_conflict_once(
    pg_session_factory, minio_store
) -> None:
    """§17-4: 人とAIが同じ版を同時に変更 → 競合を返す（片方だけが版を作る）。"""
    from tests.support.intervention import AI, HUMAN

    async with pg_session_factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.ASSETS_READY)
        script, _sb, _media = await seed_script_and_storyboard(s, minio_store, episode_id)
    ids = []
    for principal, text_ in ((HUMAN, "人の案。"), (AI, "AIの案。")):
        outcome = await submit(
            pg_session_factory,
            minio_store,
            episode_id,
            envelope(CommandType.PROPOSE_CHANGE, _script_edit(script.id, text_)),
            principal,
        )
        assert outcome.status is CommandStatus.APPLIED, outcome.result
        ids.append((principal, outcome.result["change_set_id"]))
    envs = [(p, envelope(CommandType.APPLY_CHANGE, {"change_set_id": cs})) for p, cs in ids]
    for principal, env in envs:
        await receive(pg_session_factory, episode_id, env, principal)
    results = await asyncio.gather(
        *[apply(pg_session_factory, minio_store, env.command_id) for _p, env in envs],
        return_exceptions=True,
    )
    assert not [r for r in results if isinstance(r, BaseException)], results
    applied = [r for r in results if r.status is CommandStatus.APPLIED]  # type: ignore[union-attr]
    # AI の適用は費用0・制約内だけ許される。AI が拒否された場合も、版を作るのは多くて1つ
    assert len(applied) <= 1
    async with pg_session_factory() as s:
        scripts = await ArtifactMetadataRepository(s).find_current_by_type(
            episode_id, ArtifactType.SCRIPT
        )
    assert scripts is not None
    if applied:
        assert scripts.id == applied[0].result["revision_id"]  # type: ignore[union-attr]
    else:
        assert scripts.id == script.id


# --------------------------------------------------------------------------- INV-44


async def _reserve(factory, episode_id: str, key: str):
    async with factory() as s:
        row = await ProviderReservationRepository(s).reserve(
            episode_id=episode_id,
            provider=ProviderCall.FAL_IMAGE,
            idempotency_key=key,
            input_hash="h" * 64,
            round=1,
            scene_id="sb1",
        )
        await s.commit()
        return row


async def _try_dispatch(
    factory, reservation_id: str, start: asyncio.Event, *, hold_open: float = 0.0
) -> str:
    """mark_dispatched を別接続で。``hold_open`` 秒だけ commit を遅らせる（lock を持ったまま）。"""
    await start.wait()
    async with factory() as s:
        try:
            await ProviderReservationRepository(s).mark_dispatched(reservation_id)
            await asyncio.sleep(hold_open)
            await s.commit()
            return "dispatched"
        except EpisodeHeldError:
            await s.rollback()
            return "refused"


async def _dispatched_at(factory, reservation_id: str):
    from infrastructure.db.models import ProviderReservationRow

    async with factory() as s:
        row = await s.get(ProviderReservationRow, uuid.UUID(reservation_id))
        assert row is not None
        return row.dispatched_at


async def _running_episode(factory) -> str:
    """制御行が実在する RUNNING の Episode（行が無い旧方式は gate が lock を取らない）。"""
    async with factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.ASSETS_READY)
        await InterventionStore(s).lock_control(episode_id)
        await s.commit()
    return episode_id


def _listed(result: dict) -> set[str]:
    return {i["reservation_id"] for k in ("in_flight", "unknown_outcome") for i in result[k]}


async def test_a_dispatch_in_flight_when_the_hold_is_received_is_listed_after_held(
    pg_session_factory, minio_store
) -> None:
    """INV-44: dispatch が行ロックを持ったまま commit を待つ間に hold を受け付けても、
    受付は dispatch の commit を待つ。受付が返った時点で dispatch は見えており、一覧に載る。"""
    for n in range(ROUNDS):
        episode_id = await _running_episode(pg_session_factory)
        row = await _reserve(pg_session_factory, episode_id, f"inflight-{n}")
        hold = envelope(CommandType.REQUEST_HOLD)
        start = asyncio.Event()
        dispatch = asyncio.create_task(
            _try_dispatch(pg_session_factory, row.id, start, hold_open=0.4)
        )
        start.set()
        await asyncio.sleep(0.15)  # dispatch が share lock を持った後に hold を受け付ける
        await receive(pg_session_factory, episode_id, hold)
        seen_at_receive = await _dispatched_at(pg_session_factory, row.id)
        assert await dispatch == "dispatched"
        # 受付が commit した時点で dispatch が見えていなければ、hold の一覧は送信済みを取りこぼす
        assert seen_at_receive is not None, (
            "hold was accepted before the in-flight dispatch committed"
        )
        held = await apply(pg_session_factory, minio_store, hold.command_id)
        assert held.status is CommandStatus.APPLIED
        assert row.id in _listed(held.result)
        async with pg_session_factory() as s:
            control = await InterventionStore(s).get_control(episode_id)
        assert control.control_state is ControlState.HELD


async def test_a_dispatch_after_the_hold_is_received_is_refused_and_unlisted(
    pg_session_factory, minio_store
) -> None:
    """INV-44: 受付の後に始まった dispatch は拒否され、予約は未送信のまま。一覧にも載らない。
    別接続で racing させ、結末は「送信済みで一覧にある」か「拒否で一覧に無い」のどちらかだけ。"""
    seen: set[str] = set()
    delays = [0.0, 0.0, 0.002, 0.005, 0.01, 0.0, 0.001, 0.003]
    for n, delay in enumerate(delays * 2):
        episode_id = await _running_episode(pg_session_factory)
        row = await _reserve(pg_session_factory, episode_id, f"race-{n}")
        hold = envelope(CommandType.REQUEST_HOLD)
        start = asyncio.Event()

        async def received(hold=hold, episode_id=episode_id, delay=delay, start=start):
            await start.wait()
            await asyncio.sleep(delay)
            await receive(pg_session_factory, episode_id, hold)

        tasks = asyncio.gather(_try_dispatch(pg_session_factory, row.id, start), received())
        start.set()
        dispatch, _ = await tasks
        held = await apply(pg_session_factory, minio_store, hold.command_id)
        assert held.status is CommandStatus.APPLIED
        dispatched = await _dispatched_at(pg_session_factory, row.id) is not None
        assert dispatched == (dispatch == "dispatched")  # 「両方」でも「どちらでもない」でもない
        assert (row.id in _listed(held.result)) == dispatched
        seen.add(dispatch)
        # HELD の後の新しい dispatch は必ず拒否され、未送信のまま
        after = await _reserve(pg_session_factory, episode_id, f"after-{n}")
        assert await _try_dispatch(pg_session_factory, after.id, _set()) == "refused"
        assert await _dispatched_at(pg_session_factory, after.id) is None
    assert seen <= {"dispatched", "refused"}


def _set() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event


async def test_a_legacy_episode_without_a_control_row_still_dispatches_on_postgres(
    pg_session_factory,
) -> None:
    """INV-44 の裏: 制御行の無い（日次・旧方式の）Episode は従来どおり dispatch できる。"""
    from sqlalchemy import func, select

    from infrastructure.db.models import EpisodeControlRow

    async with pg_session_factory() as s:
        episode = await EpisodeRepository(s).create(topic="legacy")
        await s.commit()
        episode_id = episode.id
    row = await _reserve(pg_session_factory, episode_id, "legacy-1")
    assert await _try_dispatch(pg_session_factory, row.id, _set()) == "dispatched"
    async with pg_session_factory() as s:
        count = await s.scalar(select(func.count()).select_from(EpisodeControlRow))
    assert count == 0
