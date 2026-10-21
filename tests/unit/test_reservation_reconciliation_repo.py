"""未照合予約の照合記録・手動確定・再送ガード（ADR-0053 / INV-67）。

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
        outcome=outcome,
        evidence="fal dashboard: COMPLETED",
        owner_approval="OWNER-20261021-A1B2C3",
        actor="owner",
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


async def test_manual_resolve_after_a_provider_confirmed_observation_still_settles_the_ledger(
    session,
) -> None:
    """監査指摘 P1: 観測だけが確定した予約が reserved のまま詰まってはいけない。"""
    _, rid = await _reserved(session)
    repo = ProviderReconciliationRepository(session)
    done = ProviderObservation(queue_status="COMPLETED", result_outcome="ok")
    verdict, _ = await repo.record_observation(rid, done)
    assert verdict is V.CONFIRMED_SUCCESS
    assert await repo.resolve_manual(rid, _resolution("success")) is True
    row = await ProviderReservationRepository(session).get(rid)
    assert row is not None and row.status is ReservationStatus.SPENT
    assert await repo.resolve_manual(rid, _resolution("success")) is False  # 以後は冪等


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


# ---- (e) 照合記録の無い dispatched な reserved（独立レビュー P2-a）


async def _reserve_dispatch(session, episode_id: str, key: str, *, dispatch: bool) -> str:
    repo = ProviderReservationRepository(session)
    r = await repo.reserve(
        episode_id=episode_id,
        provider=ProviderCall.FAL_VIDEO,
        idempotency_key=key,
        input_hash="h1",
        round=1,
        scene_id="sb1",
    )
    if dispatch:
        await repo.mark_dispatched(r.id)
    await session.commit()
    return r.id


async def test_dispatched_reserved_without_any_verdict_blocks_a_new_key(session) -> None:
    """観測ゼロでも、呼んだ可能性のある reserved は UNKNOWN として再送を止める。"""
    episode = await EpisodeRepository(session).create(topic="t")
    await _reserve_dispatch(session, episode.id, "k-old", dispatch=True)
    assert (await ProviderReconciliationRepository(session).list_unreconciled())[
        0
    ].verdict is V.UNKNOWN  # list と guard の読み方が一致している
    with pytest.raises(ResubmitRejectedError):
        await _try_new_key(session, episode.id)


async def test_never_dispatched_reserved_does_not_block_a_new_key(session) -> None:
    """呼んでいない証拠のある reserved は従来どおり（別経路 find_unreconciled が扱う）。"""
    episode = await EpisodeRepository(session).create(topic="t")
    await _reserve_dispatch(session, episode.id, "k-old", dispatch=False)
    await _try_new_key(session, episode.id)


async def test_same_key_is_not_a_resubmit_even_if_dispatched(session) -> None:
    """同じ idempotency key は新規送信ではない: ガード自身は拒否しない（一意制約が止める）。"""
    from sqlalchemy.exc import IntegrityError

    episode = await EpisodeRepository(session).create(topic="t")
    await _reserve_dispatch(session, episode.id, "k-old", dispatch=True)
    with pytest.raises(IntegrityError):
        await ProviderReservationRepository(session).reserve(
            episode_id=episode.id,
            provider=ProviderCall.FAL_VIDEO,
            idempotency_key="k-old",
            input_hash="h1",
            round=1,
            scene_id="sb1",
        )


async def test_terminal_reservations_without_verdict_do_not_block(session) -> None:
    """spent（conservative 確定）・abandoned は照合記録が無くても従来どおり次ラウンド可。"""
    episode = await EpisodeRepository(session).create(topic="t")
    repo = ProviderReservationRepository(session)
    rid = await _reserve_dispatch(session, episode.id, "k-spent", dispatch=True)
    await repo.mark_spent(rid, raw_output_key=None, reconciled_by="conservative")
    await session.commit()
    await _try_new_key(session, episode.id)
    rid2 = await _reserve_dispatch(session, episode.id, "k-aband", dispatch=False)
    await repo.abandon(rid2, reconciled_by="operator:x")
    await session.commit()
    await _try_new_key(session, episode.id)
