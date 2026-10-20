"""``scripts/reconcile-reservations.py``（ADR-0051）。sqlite + 注入した fake の status 関数。

理由は docs/testing/reservation-reconciliation-rationale.md。provider へは接続しない。
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

from contracts.states import ProviderCall, ReconciliationVerdict, ReservationStatus
from domain.provider.reconciliation import ProviderObservation
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReconciliationRepository,
    ProviderReservationRepository,
)

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "reconcile-reservations.py"
V = ReconciliationVerdict
TOKEN = "OWNER-SECRET-TOKEN-123"


def _load():
    spec = importlib.util.spec_from_file_location("reconcile_reservations", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _reserved(session_factory) -> str:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic="t")
        repo = ProviderReservationRepository(s)
        r = await repo.reserve(
            episode_id=ep.id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="k",
            input_hash="h",
            round=1,
            scene_id="sb4",
        )
        await repo.mark_dispatched(r.id)
        await repo.record_provider_job_ref(r.id, json.dumps({"v": 1, "request_id": "req-9"}))
        await s.commit()
        return r.id


async def _status(session_factory, rid: str) -> ReservationStatus:
    async with session_factory() as s:
        row = await ProviderReservationRepository(s).get(rid)
        assert row is not None
        return row.status


async def _audit_rows(session_factory) -> int:
    from sqlalchemy import func, select

    from infrastructure.db.models import ProviderReservationReconciliationRow as Row

    async with session_factory() as s:
        return int((await s.execute(select(func.count()).select_from(Row))).scalar_one())


async def test_list_is_structured_json_with_age(session_factory) -> None:
    rid = await _reserved(session_factory)
    code, out = await _load().run(["list"], session_factory=session_factory)
    assert code == 0
    json.dumps(out)  # JSON 化できる
    assert out["count"] == 1 and out["items"][0]["reservation_id"] == rid
    assert out["items"][0]["provider_request_id"] == "req-9"
    assert "age_seconds" in out["items"][0]


async def test_reconcile_is_dry_run_by_default_and_writes_nothing(session_factory) -> None:
    await _reserved(session_factory)
    calls: list[str] = []

    async def status_fn(view):
        calls.append(view.reservation_id)
        return ProviderObservation(queue_status="IN_PROGRESS")

    code, out = await _load().run(
        ["reconcile"], session_factory=session_factory, status_fn=status_fn
    )
    assert code == 0 and out["dry_run"] is True
    assert out["plans"][0]["observed"] == "still_running" and out["plans"][0]["would_write"] is True
    assert len(calls) == 1
    assert await _audit_rows(session_factory) == 0


async def test_reconcile_apply_twice_is_idempotent(session_factory) -> None:
    rid = await _reserved(session_factory)

    async def status_fn(view):
        return ProviderObservation(queue_status="COMPLETED", error="HTTP 403")

    mod = _load()
    for _ in range(2):
        code, out = await mod.run(
            ["reconcile", "--apply"], session_factory=session_factory, status_fn=status_fn
        )
        assert code == 0
    assert await _audit_rows(session_factory) == 1
    assert await _status(session_factory, rid) is ReservationStatus.RESERVED
    async with session_factory() as s:
        assert await ProviderReconciliationRepository(s).latest_verdict(rid) is V.UNKNOWN


async def test_reconcile_status_fn_exception_is_unknown_not_failure(session_factory) -> None:
    await _reserved(session_factory)

    async def boom(view):
        raise RuntimeError("secret-bearing message must not leak")

    code, out = await _load().run(["reconcile"], session_factory=session_factory, status_fn=boom)
    assert out["plans"][0]["observed"] == "unknown"
    assert "secret-bearing" not in json.dumps(out)


async def test_reconcile_without_a_status_source_refuses(session_factory) -> None:
    await _reserved(session_factory)
    code, out = await _load().run(["reconcile"], session_factory=session_factory)
    assert code == 3 and "error" in out


async def test_resolve_requires_evidence_and_owner_approval(session_factory) -> None:
    rid = await _reserved(session_factory)
    mod = _load()
    base = ["resolve", "--reservation-id", rid, "--outcome", "success", "--actor", "me", "--apply"]
    for extra in ([], ["--evidence", "e"], ["--owner-approval", TOKEN]):
        code, out = await mod.run([*base, *extra], session_factory=session_factory)
        assert code == 2 and "error" in out
    assert await _status(session_factory, rid) is ReservationStatus.RESERVED
    assert await _audit_rows(session_factory) == 0


async def test_resolve_dry_run_writes_nothing(session_factory) -> None:
    rid = await _reserved(session_factory)
    argv = ["resolve", "--reservation-id", rid, "--outcome", "success", "--actor", "me"]
    argv += ["--evidence", "e", "--owner-approval", TOKEN]
    code, out = await _load().run(argv, session_factory=session_factory)
    assert code == 0 and out["dry_run"] is True and out["would_write"] is True
    assert await _audit_rows(session_factory) == 0
    assert await _status(session_factory, rid) is ReservationStatus.RESERVED


async def test_resolve_apply_audits_without_leaking_the_token(session_factory) -> None:
    rid = await _reserved(session_factory)
    argv = ["resolve", "--reservation-id", rid, "--outcome", "failure", "--actor", "me", "--apply"]
    argv += ["--evidence", "dashboard shows failed", "--owner-approval", TOKEN]
    mod = _load()
    code, out = await mod.run(argv, session_factory=session_factory)
    assert code == 0 and out["wrote"] is True
    assert TOKEN not in json.dumps(out)
    assert await _audit_rows(session_factory) == 1
    assert await _status(session_factory, rid) is ReservationStatus.SPENT
    code, again = await mod.run(argv, session_factory=session_factory)  # 冪等
    assert code == 0 and again["wrote"] is False
    assert await _audit_rows(session_factory) == 1


async def test_audit_row_never_stores_the_raw_token(session_factory) -> None:
    from sqlalchemy import select

    from infrastructure.db.models import ProviderReservationReconciliationRow as Row

    rid = await _reserved(session_factory)
    argv = ["resolve", "--reservation-id", rid, "--outcome", "success", "--actor", "me", "--apply"]
    argv += ["--evidence", "e", "--owner-approval", TOKEN]
    await _load().run(argv, session_factory=session_factory)
    async with session_factory() as s:
        row = (await s.scalars(select(Row))).one()
    assert TOKEN not in repr(row.__dict__)
    assert row.owner_approval_fingerprint and row.actor == "me" and row.evidence == "e"


def test_script_has_no_network_or_provider_imports() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    for banned in ("httpx", "fal_queue", "requests", "googleapiclient", "urllib.request"):
        assert banned not in text
