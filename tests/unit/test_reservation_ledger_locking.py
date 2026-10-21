"""台帳の並行制御（独立レビュー P2-b / ADR-0053）。

理由は docs/testing/reservation-reconciliation-rationale.md。
SQLite は書き込みを直列化し FOR UPDATE も出さない。ここで確かめられるのは発行する文まで。
実 PostgreSQL での競合は tests/integration/test_reservation_ledger_concurrency.py
（TEST_DATABASE_URL が無ければ skip。この環境では未実行）。
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from contracts.states import ProviderCall
from domain.provider.reconciliation import validate_manual_resolution
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReconciliationRepository,
    ProviderReservationRepository,
    input_lock_key,
    reservation_for_update_stmt,
)

REF = json.dumps(
    {"v": 1, "request_id": "r", "endpoint": "e", "status_url": "u", "response_url": "r"}
)


def test_reservation_row_lock_statement_uses_for_update() -> None:
    sql = str(reservation_for_update_stmt(uuid.uuid4()).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql


def test_input_lock_key_is_stable_distinct_and_fits_bigint() -> None:
    a = input_lock_key("ep", ProviderCall.FAL_VIDEO, "sb1", "h1")
    assert a == input_lock_key("ep", ProviderCall.FAL_VIDEO, "sb1", "h1")
    others = {
        input_lock_key("ep2", ProviderCall.FAL_VIDEO, "sb1", "h1"),
        input_lock_key("ep", ProviderCall.FAL_IMAGE, "sb1", "h1"),
        input_lock_key("ep", ProviderCall.FAL_VIDEO, "sb2", "h1"),
        input_lock_key("ep", ProviderCall.FAL_VIDEO, None, "h1"),
        input_lock_key("ep", ProviderCall.FAL_VIDEO, "sb1", "h2"),
    }
    assert a not in others and len(others) == 5
    assert all(-(2**63) <= k < 2**63 for k in (a, *others))


class _Recorder:
    """session.execute に渡された文を記録する薄い spy（実行は本物に委譲）。"""

    def __init__(self, session) -> None:
        self.statements: list[object] = []
        real = session.execute

        async def execute(stmt, *args, **kwargs):
            self.statements.append(stmt)
            return await real(stmt, *args, **kwargs)

        session.execute = execute  # type: ignore[method-assign]


async def _dispatched(session) -> tuple[str, str]:
    episode = await EpisodeRepository(session).create(topic="t")
    repo = ProviderReservationRepository(session)
    r = await repo.reserve(
        episode_id=episode.id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key="k1",
        input_hash="h1",
        round=1,
        scene_id="sb1",
    )
    await repo.mark_dispatched(r.id)
    await repo.record_provider_job_ref(r.id, REF)
    await session.commit()
    return episode.id, r.id


async def test_resolve_manual_locks_the_reservation_row(session) -> None:
    _, rid = await _dispatched(session)
    spy = _Recorder(session)
    resolution = validate_manual_resolution(
        outcome="success", evidence="e", owner_approval="OWNER-20261021-A1B2C3", actor="a"
    )
    await ProviderReconciliationRepository(session).resolve_manual(rid, resolution)
    assert any(getattr(s, "_for_update_arg", None) is not None for s in spy.statements)


async def test_reserve_takes_the_input_advisory_lock_only_on_postgresql(session) -> None:
    episode = await EpisodeRepository(session).create(topic="t")
    calls: list[str] = []

    class _FakePgSession:
        """dialect が postgresql の session。lock 文が発行されることだけを見る。"""

        def __init__(self, inner) -> None:
            self._inner = inner

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, stmt, *args, **kwargs):
            sql = str(stmt)
            if "pg_advisory_xact_lock" in sql:
                calls.append(sql)
                return None
            return await self._inner.execute(stmt, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    await ProviderReservationRepository(session).reserve(
        episode_id=episode.id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key="sqlite-k",
        input_hash="h1",
        round=1,
        scene_id="sb1",
    )
    assert calls == []  # SQLite では出さない
    await ProviderReservationRepository(_FakePgSession(session)).reserve(  # type: ignore[arg-type]
        episode_id=episode.id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key="pg-k",
        input_hash="h2",
        round=1,
        scene_id="sb1",
    )
    assert len(calls) == 1
