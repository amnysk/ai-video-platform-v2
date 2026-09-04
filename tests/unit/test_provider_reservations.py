"""予約台帳（ADR-0013）。INV-15 の機械検査。

「未照合の予約を自動で再送も解放もしない」を、遷移表の**辺の不在**と
DB制約で検査する。
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from contracts.states import FailureClass, ProviderCall, ReservationStatus
from domain.errors import InvalidTransitionError
from domain.provider.reservations import (
    RESERVATION_TRANSITIONS,
    ReservationEvent,
    transition_reservation,
)
from domain.script.identity import idempotency_key, script_input_hash
from infrastructure.db.models import Base, ProviderReservationRow
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository

INPUT_KWARGS = dict(
    episode_id="e1",
    topic="ローマ水道",
    artifact_type="script",
    target_schema_version="1.0",
    prompt_template_id="write_script",
    prompt_template_version="3",
    generator_id="codex:gpt-5",
)


def test_reservation_table_has_no_automatic_exit_from_reserved() -> None:
    """``reserved`` から出る辺は evidence 照合と人手事象だけ。

    ここに自動事象の辺が増えたら INV-15 が壊れている。
    """
    events_out_of_reserved = {
        event for (state, event) in RESERVATION_TRANSITIONS if state is ReservationStatus.RESERVED
    }
    assert events_out_of_reserved == {
        ReservationEvent.EVIDENCE_RECONCILED,
        ReservationEvent.OPERATOR_CONFIRMED_SPENT,
        ReservationEvent.OPERATOR_ABANDONED,
    }
    # 終端状態からは出られない。
    assert not [
        state for (state, _e) in RESERVATION_TRANSITIONS if state is not ReservationStatus.RESERVED
    ]


def test_transition_reservation_rejects_edges_not_in_the_table() -> None:
    assert (
        transition_reservation(ReservationStatus.RESERVED, ReservationEvent.EVIDENCE_RECONCILED)
        is ReservationStatus.SPENT
    )
    rejected = transition_reservation(ReservationStatus.SPENT, ReservationEvent.OPERATOR_ABANDONED)
    assert not isinstance(rejected, ReservationStatus)


def test_idempotency_key_is_stable_for_the_same_round_and_changes_across_rounds() -> None:
    h = script_input_hash(**INPUT_KWARGS)
    a = idempotency_key(provider=ProviderCall.CODEX_SCRIPT.value, input_hash=h, round=1)
    b = idempotency_key(provider=ProviderCall.CODEX_SCRIPT.value, input_hash=h, round=1)
    c = idempotency_key(provider=ProviderCall.CODEX_SCRIPT.value, input_hash=h, round=2)
    assert a == b
    assert a != c


def test_input_hash_ignores_round_and_time() -> None:
    """input_hash の材料にラウンド・時刻・job_id を混ぜない（ADR-0012）。"""
    import inspect as _inspect

    first = script_input_hash(**INPUT_KWARGS)
    second = script_input_hash(**INPUT_KWARGS)
    assert first == second
    assert len(first) == 64

    params = set(_inspect.signature(script_input_hash).parameters)
    assert params == set(INPUT_KWARGS)
    assert not (params & {"round", "attempt", "now", "hostname", "job_id"})

    changed = dict(INPUT_KWARGS, topic="別の話題")
    assert script_input_hash(**changed) != first


@pytest_asyncio.fixture
async def file_engine(tmp_path):
    """ファイルバックの SQLite + NullPool。

    conftest の StaticPool（単一接続共有）だと別セッションを開いても
    commit の証明にならないので、**別の物理接続**から見えることを確かめる。
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'reservations.db'}"
    engine = create_async_engine(url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine, url
    await engine.dispose()


async def _episode(session_factory) -> str:
    async with session_factory() as s:
        episode = await EpisodeRepository(s).create(topic="t")
        await s.commit()
        return episode.id


def _key(round_: int = 1) -> str:
    return idempotency_key(
        provider=ProviderCall.CODEX_SCRIPT.value,
        input_hash=script_input_hash(**INPUT_KWARGS),
        round=round_,
    )


async def test_reservation_is_visible_from_another_connection_before_the_generator_runs(
    file_engine,
) -> None:
    engine, url = file_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    episode_id = await _episode(factory)

    async with factory() as s:
        await ProviderReservationRepository(s).reserve(
            episode_id=episode_id,
            job_id=None,
            provider=ProviderCall.CODEX_SCRIPT,
            idempotency_key=_key(),
            input_hash=script_input_hash(**INPUT_KWARGS),
            round=1,
        )
        await s.commit()

    other = create_async_engine(url, poolclass=NullPool)
    try:
        async with async_sessionmaker(other, expire_on_commit=False)() as s:
            found = await ProviderReservationRepository(s).find_by_key(_key())
        assert found is not None
        assert found.status is ReservationStatus.RESERVED
        assert found.dispatched_at is None
    finally:
        await other.dispose()


async def test_crash_after_dispatch_leaves_the_reservation_unreconciled(file_engine) -> None:
    engine, _url = file_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    episode_id = await _episode(factory)

    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        async with factory() as s:
            repo = ProviderReservationRepository(s)
            res = await repo.reserve(
                episode_id=episode_id,
                job_id=None,
                provider=ProviderCall.CODEX_SCRIPT,
                idempotency_key=_key(),
                input_hash=script_input_hash(**INPUT_KWARGS),
                round=1,
            )
            await s.commit()
            await repo.mark_dispatched(res.id)
            await s.commit()
            raise Crash("generator process died")

    async with factory() as s:
        row = await ProviderReservationRepository(s).find_by_key(_key())
    assert row is not None
    assert row.status is ReservationStatus.RESERVED
    assert row.dispatched_at is not None
    assert row.reconciled_at is None
    assert row.raw_output_key is None


async def test_unreconciled_reservation_is_never_resent(file_engine) -> None:
    engine, _url = file_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    episode_id = await _episode(factory)

    async with factory() as s:
        repo = ProviderReservationRepository(s)
        res = await repo.reserve(
            episode_id=episode_id,
            job_id=None,
            provider=ProviderCall.CODEX_SCRIPT,
            idempotency_key=_key(),
            input_hash=script_input_hash(**INPUT_KWARGS),
            round=1,
        )
        await s.commit()
        await repo.mark_dispatched(res.id)
        await repo.record_failure(
            res.id, failure_class=FailureClass.RETRYABLE, error_summary="timeout"
        )
        await s.commit()

    async with factory() as s:
        stale = await ProviderReservationRepository(s).find_unreconciled(
            episode_id, ProviderCall.CODEX_SCRIPT
        )
    assert [r.idempotency_key for r in stale] == [_key()]
    # 失敗を記録しても状態は reserved のまま（自動で解放しない）。
    assert stale[0].status is ReservationStatus.RESERVED


async def test_reconciled_reservation_is_no_longer_unreconciled(file_engine) -> None:
    engine, _url = file_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    episode_id = await _episode(factory)

    async with factory() as s:
        repo = ProviderReservationRepository(s)
        res = await repo.reserve(
            episode_id=episode_id,
            job_id=None,
            provider=ProviderCall.CODEX_SCRIPT,
            idempotency_key=_key(),
            input_hash=script_input_hash(**INPUT_KWARGS),
            round=1,
        )
        await s.commit()
        await repo.mark_dispatched(res.id)
        spent = await repo.mark_spent(res.id, raw_output_key="provider-raw/x.txt")
        await s.commit()
        assert spent.status is ReservationStatus.SPENT
        assert spent.reconciled_by == "evidence"
        assert await repo.find_unreconciled(episode_id, ProviderCall.CODEX_SCRIPT) == []

        with pytest.raises(InvalidTransitionError):
            await repo.mark_spent(res.id, raw_output_key="provider-raw/x.txt")


async def test_duplicate_idempotency_key_is_rejected_by_the_database(file_engine) -> None:
    engine, _url = file_engine
    factory = async_sessionmaker(engine, expire_on_commit=False)
    episode_id = await _episode(factory)

    async with factory() as s:
        await ProviderReservationRepository(s).reserve(
            episode_id=episode_id,
            job_id=None,
            provider=ProviderCall.CODEX_SCRIPT,
            idempotency_key=_key(),
            input_hash=script_input_hash(**INPUT_KWARGS),
            round=1,
        )
        await s.commit()

    with pytest.raises(IntegrityError):
        async with factory() as s:
            s.add(
                ProviderReservationRow(
                    id=uuid.uuid4(),
                    episode_id=uuid.UUID(episode_id),
                    provider=ProviderCall.CODEX_SCRIPT.value,
                    idempotency_key=_key(),
                    input_hash=script_input_hash(**INPUT_KWARGS),
                    round=1,
                    status=ReservationStatus.RESERVED.value,
                )
            )
            await s.commit()


# --- 保守的照合（ADR-0013 §保守的照合）。統合テストが暴いた設計の緊張点への解 ---


async def test_failed_call_can_be_conservatively_reconciled_without_evidence(session) -> None:
    """呼び出しが**戻ってきた上で**失敗した予約は、保守的に spent へ確定できる。

    未照合のまま残すと次ラウンドが永久にブロックされ、
    「retryable な失敗を安全に retry する」が成立しない。
    課金されたかは不明なので、**課金された前提**で確定する（安全側）。
    """
    from contracts.states import FailureClass, ProviderCall, ReservationStatus
    from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository

    episodes = EpisodeRepository(session)
    reservations = ProviderReservationRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    reservation = await reservations.reserve(
        episode_id=episode.id,
        job_id=None,
        provider=ProviderCall.CODEX_SCRIPT,
        idempotency_key="k-conservative",
        input_hash="h" * 64,
        round=1,
    )
    await session.commit()
    await reservations.mark_dispatched(reservation.id)
    await session.commit()

    settled = await reservations.mark_spent(
        reservation.id,
        raw_output_key=None,
        reconciled_by="conservative",
        failure_class=FailureClass.RETRYABLE,
        error_summary="ProviderTimeoutError: simulated",
    )
    await session.commit()

    assert settled.status is ReservationStatus.SPENT
    assert settled.raw_output_key is None
    assert settled.reconciled_by == "conservative"

    # 確定済みなので、次ラウンドを妨げない
    stale = await reservations.find_unreconciled(
        episode_id=episode.id, provider=ProviderCall.CODEX_SCRIPT
    )
    assert list(stale) == []


async def test_conservatively_reconciled_call_is_never_resent(session) -> None:
    """保守的に確定した予約も「そのキーの呼び出し」は二度としない。"""
    from contracts.states import FailureClass, ProviderCall
    from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository

    episodes = EpisodeRepository(session)
    reservations = ProviderReservationRepository(session)
    episode = await episodes.create(topic="t")
    await session.commit()

    reservation = await reservations.reserve(
        episode_id=episode.id,
        job_id=None,
        provider=ProviderCall.CODEX_SCRIPT,
        idempotency_key="k-once",
        input_hash="h" * 64,
        round=1,
    )
    await session.commit()
    await reservations.mark_spent(
        reservation.id,
        raw_output_key=None,
        reconciled_by="conservative",
        failure_class=FailureClass.RETRYABLE,
        error_summary="x",
    )
    await session.commit()

    found = await reservations.find_by_key("k-once")
    assert found is not None
    assert found.raw_output_key is None, "生出力が無いので再パースの材料も無い"
