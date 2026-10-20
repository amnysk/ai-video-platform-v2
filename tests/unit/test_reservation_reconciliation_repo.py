"""未照合予約の照合記録・手動確定・再送ガード（ADR-0051 / INV-66）。

理由は docs/testing/reservation-reconciliation-rationale.md。fake のみ。provider へは出ない。
"""

from __future__ import annotations

import json
import uuid

import pytest

from contracts.states import ProviderCall, ReconciliationVerdict, ReservationStatus
from domain.errors import ResubmitRejectedError
from domain.provider.reconciliation import (
    ManualResolutionError,
    ProviderObservation,
    validate_manual_resolution,
)
from infrastructure.db.repositories import (
    EpisodeRepository,
    ProviderReconciliationRepository,
    ProviderReservationRepository,
)

V = ReconciliationVerdict
REF = json.dumps(
    {"v": 1, "request_id": "req-1", "endpoint": "e", "status_url": "u", "response_url": "r"}
)


async def _reserved(
    session, *, key="k1", input_hash="h1", round_=1, scene="sb1"
) -> tuple[str, str]:
    episode = await EpisodeRepository(session).create(topic="t")
    repo = ProviderReservationRepository(session)
    r = await repo.reserve(
        episode_id=episode.id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key=key,
        input_hash=input_hash,
        round=round_,
        scene_id=scene,
    )
    await repo.mark_dispatched(r.id)
    await repo.record_provider_job_ref(r.id, REF)
    await session.commit()
    return episode.id, r.id


def _resolution(outcome="success"):
    return validate_manual_resolution(
        outcome=outcome, evidence="fal dashboard: COMPLETED", owner_approval="tok", actor="owner"
    )


async def test_list_shows_unreconciled_with_age_and_request_id(session) -> None:
    _, rid = await _reserved(session)
    views = await ProviderReconciliationRepository(session).list_unreconciled()
    [v] = views
    assert v.reservation_id == rid
    assert v.provider_request_id == "req-1"
    assert v.verdict is V.UNKNOWN  # 観測前は UNKNOWN（失敗ではない）
    assert v.age_seconds >= 0 and v.dispatched is True


async def test_summary_counts_by_provider_and_episode(session) -> None:
    ep, _ = await _reserved(session)
    summary = await ProviderReconciliationRepository(session).unreconciled_summary()
    assert summary["count"] == 1
    assert summary["by_provider"] == {"fal_video": 1}
    assert summary["by_episode"] == {ep: 1}


async def test_observation_is_idempotent_and_leaves_the_ledger_untouched(session) -> None:
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    obs = ProviderObservation(queue_status="IN_PROGRESS")
    assert await repo.record_observation(rid, obs) == (V.STILL_RUNNING, True)
    assert await repo.record_observation(rid, obs) == (V.STILL_RUNNING, False)
    await session.commit()
    row = await ProviderReservationRepository(session).get(rid)
    assert row is not None and row.status is ReservationStatus.RESERVED


async def test_unknown_observation_stays_unknown_and_never_becomes_failure(session) -> None:
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    unreadable = ProviderObservation(queue_status="COMPLETED", error="HTTP 403")
    assert await repo.record_observation(rid, unreadable) == (V.UNKNOWN, True)
    assert await repo.record_observation(rid, unreadable) == (V.UNKNOWN, False)
    assert (
        await ProviderReservationRepository(session).get(rid)
    ).status is ReservationStatus.RESERVED  # type: ignore[union-attr]


async def test_manual_resolve_records_audit_row_and_is_idempotent(session) -> None:
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    assert await repo.resolve_manual(rid, _resolution("success")) is True
    assert await repo.resolve_manual(rid, _resolution("success")) is False
    await session.commit()
    row = await ProviderReservationRepository(session).get(rid)
    assert row is not None
    assert row.status is ReservationStatus.SPENT and row.reconciled_by == "operator:owner"
    assert await repo.latest_verdict(rid) is V.CONFIRMED_SUCCESS


async def test_manual_resolve_cannot_flip_a_confirmed_outcome(session) -> None:
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    await repo.resolve_manual(rid, _resolution("success"))
    with pytest.raises(ManualResolutionError):
        await repo.resolve_manual(rid, _resolution("failure"))


async def test_manual_cancel_abandons_the_reservation(session) -> None:
    _, rid = await _reserved(session)
    await ProviderReconciliationRepository(session).resolve_manual(rid, _resolution("cancelled"))
    row = await ProviderReservationRepository(session).get(rid)
    assert row is not None and row.status is ReservationStatus.ABANDONED


async def test_provider_observation_cannot_override_a_manual_confirmation(session) -> None:
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    await repo.resolve_manual(rid, _resolution("success"))
    verdict, wrote = await repo.record_observation(
        rid, ProviderObservation(queue_status="IN_QUEUE")
    )
    assert (verdict, wrote) == (V.CONFIRMED_SUCCESS, False)


# ---- (d) 再送ガード（契約）


async def _try_new_key(session, episode_id: str) -> None:
    await ProviderReservationRepository(session).reserve(
        episode_id=episode_id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key=f"new-{uuid.uuid4().hex}",
        input_hash="h1",
        round=2,
        scene_id="sb1",
    )


async def test_confirmed_success_can_never_be_resubmitted_under_a_new_key(session) -> None:
    ep, rid = await _reserved(session)
    await ProviderReconciliationRepository(session).resolve_manual(rid, _resolution("success"))
    await session.commit()
    with pytest.raises(ResubmitRejectedError):
        await _try_new_key(session, ep)


async def test_unknown_reservation_can_never_be_resubmitted_under_a_new_key(session) -> None:
    ep, rid = await _reserved(session)
    await ProviderReconciliationRepository(session).record_observation(
        rid, ProviderObservation(queue_status="COMPLETED", error="HTTP 403")
    )
    await session.commit()
    with pytest.raises(ResubmitRejectedError):
        await _try_new_key(session, ep)


async def test_confirmed_failure_may_start_a_new_round(session) -> None:
    ep, rid = await _reserved(session)
    await ProviderReconciliationRepository(session).resolve_manual(rid, _resolution("failure"))
    await session.commit()
    await _try_new_key(session, ep)  # 失敗確定なら従来どおり次ラウンドへ進める


async def test_a_different_input_is_not_blocked(session) -> None:
    ep, rid = await _reserved(session)
    await ProviderReconciliationRepository(session).resolve_manual(rid, _resolution("success"))
    await session.commit()
    await ProviderReservationRepository(session).reserve(
        episode_id=ep,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key="other",
        input_hash="h2",
        round=1,
        scene_id="sb1",
    )
