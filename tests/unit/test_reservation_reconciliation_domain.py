"""未照合予約の判定・遷移・再送ガード（ADR-0053 / INV-67）。純粋関数の検査。

理由は docs/testing/reservation-reconciliation-rationale.md。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from contracts.states import ProviderCall, ReconciliationVerdict, ReservationStatus
from domain.provider.reconciliation import (
    ManualResolutionError,
    ProviderObservation,
    UnreconciledView,
    advance_verdict,
    blocks_new_round,
    classify_observation,
    request_id_from_ref,
    summarize_unreconciled,
    validate_manual_resolution,
)

V = ReconciliationVerdict
NOW = datetime(2026, 10, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    ("obs", "expected"),
    [
        (ProviderObservation(queue_status="IN_QUEUE"), V.STILL_RUNNING),
        (ProviderObservation(queue_status="IN_PROGRESS"), V.STILL_RUNNING),
        (ProviderObservation(queue_status="COMPLETED", result_outcome="ok"), V.CONFIRMED_SUCCESS),
        (
            ProviderObservation(queue_status="COMPLETED", result_outcome="failed"),
            V.CONFIRMED_FAILED,
        ),
        (ProviderObservation(queue_status="CANCELLED"), V.CANCELLED),
        # COMPLETED でも body を読めていなければ成否は分からない（本番の 403 の形）
        (ProviderObservation(queue_status="COMPLETED"), V.UNKNOWN),
        (ProviderObservation(queue_status=None, error="HTTP 403"), V.UNKNOWN),
        (ProviderObservation(queue_status="SOMETHING_NEW"), V.UNKNOWN),
        # 通信エラー・認可エラーは「失敗」ではない
        (ProviderObservation(queue_status="IN_PROGRESS", error="timeout"), V.UNKNOWN),
    ],
)
def test_classification(obs: ProviderObservation, expected: V) -> None:
    assert classify_observation(obs) is expected


def test_no_observation_is_never_classified_as_failed() -> None:
    """どんな不完全な観測でも CONFIRMED_FAILED にならない（UNKNOWN は失敗ではない）。"""
    for status in (None, "", "COMPLETED", "weird"):
        for error in (None, "boom"):
            assert (
                classify_observation(ProviderObservation(queue_status=status, error=error))
                is not V.CONFIRMED_FAILED
            )


ALL = [None, *V]


@pytest.mark.parametrize("current", ALL)
@pytest.mark.parametrize("observed", list(V))
def test_advance_is_idempotent(current: V | None, observed: V) -> None:
    first = advance_verdict(current, observed)
    again = advance_verdict(first.verdict, observed)
    assert again.verdict is first.verdict
    assert again.changed is False


def test_unknown_observation_never_overwrites_knowledge() -> None:
    for known in (V.STILL_RUNNING, V.CONFIRMED_SUCCESS, V.CONFIRMED_FAILED, V.CANCELLED):
        step = advance_verdict(known, V.UNKNOWN)
        assert step.verdict is known
        assert step.changed is False
        assert step.conflict is None


def test_unknown_stays_unknown_without_new_evidence() -> None:
    assert advance_verdict(V.UNKNOWN, V.UNKNOWN).verdict is V.UNKNOWN
    assert advance_verdict(None, V.UNKNOWN).verdict is V.UNKNOWN


def test_terminal_verdicts_never_flip_to_another_terminal() -> None:
    step = advance_verdict(V.CONFIRMED_SUCCESS, V.CONFIRMED_FAILED)
    assert step.verdict is V.CONFIRMED_SUCCESS
    assert step.changed is False
    assert step.conflict is not None  # 食い違いは通報する（黙って上書きしない）


def test_running_progresses_to_terminal_but_not_back() -> None:
    assert advance_verdict(V.STILL_RUNNING, V.CONFIRMED_SUCCESS).changed is True
    step = advance_verdict(V.CONFIRMED_SUCCESS, V.STILL_RUNNING)
    assert step.verdict is V.CONFIRMED_SUCCESS and step.changed is False


@pytest.mark.parametrize(
    ("status", "verdict", "blocked"),
    [
        (ReservationStatus.SPENT, V.CONFIRMED_SUCCESS, True),  # 課金済みで結果がある
        (ReservationStatus.RESERVED, V.CONFIRMED_SUCCESS, True),
        (ReservationStatus.RESERVED, V.UNKNOWN, True),
        (ReservationStatus.RESERVED, V.STILL_RUNNING, True),
        (ReservationStatus.SPENT, V.CONFIRMED_FAILED, False),  # 失敗確定は次ラウンド可
        (ReservationStatus.ABANDONED, V.CANCELLED, False),
    ],
)
def test_blocks_new_round(status: ReservationStatus, verdict: V, blocked: bool) -> None:
    assert (blocks_new_round(status, verdict) is not None) is blocked


def test_manual_resolution_requires_evidence_and_owner_approval() -> None:
    ok = validate_manual_resolution(
        outcome="success",
        evidence="fal dashboard shows COMPLETED",
        owner_approval="tok-1",
        actor="yoshiki",
    )
    assert ok.verdict is V.CONFIRMED_SUCCESS
    assert ok.reconciled_by == "operator:yoshiki"
    for bad in (
        dict(evidence="", owner_approval="tok", actor="a"),
        dict(evidence="   ", owner_approval="tok", actor="a"),
        dict(evidence="e", owner_approval="", actor="a"),
        dict(evidence="e", owner_approval="tok", actor=""),
    ):
        with pytest.raises(ManualResolutionError):
            validate_manual_resolution(outcome="failure", **bad)  # type: ignore[arg-type]
    with pytest.raises(ManualResolutionError):
        validate_manual_resolution(outcome="unknown", evidence="e", owner_approval="t", actor="a")


def test_owner_approval_is_stored_as_a_fingerprint_not_the_token() -> None:
    r = validate_manual_resolution(
        outcome="failure", evidence="e", owner_approval="super-secret-token", actor="a"
    )
    assert "super-secret-token" not in r.owner_approval_fingerprint
    assert len(r.owner_approval_fingerprint) == 16


def test_request_id_from_ref() -> None:
    ref = '{"v":1,"request_id":"abc-123","status_url":"https://queue.example.invalid/x"}'
    assert request_id_from_ref(ref) == "abc-123"
    assert request_id_from_ref(None) is None
    assert request_id_from_ref("not json") is None


def _view(i: int, provider: ProviderCall, episode: str, verdict: V, age_h: int) -> UnreconciledView:
    return UnreconciledView(
        reservation_id=f"r{i}",
        episode_id=episode,
        job_id=None,
        provider=provider,
        scene_id="sb1",
        provider_request_id=None,
        reserved_at=NOW - timedelta(hours=age_h),
        age_seconds=age_h * 3600,
        dispatched=True,
        verdict=verdict,
        estimated_cost_usd=None,
    )


def test_summary_is_collector_friendly() -> None:
    views = [
        _view(1, ProviderCall.FAL_VIDEO, "e1", V.UNKNOWN, 5),
        _view(2, ProviderCall.FAL_VIDEO, "e1", V.STILL_RUNNING, 2),
        _view(3, ProviderCall.FAL_IMAGE, "e2", V.CONFIRMED_SUCCESS, 1),
    ]
    s = summarize_unreconciled(views)
    assert s["count"] == 3
    assert s["by_provider"] == {"fal_video": 2, "fal_image": 1}
    assert s["by_episode"] == {"e1": 2, "e2": 1}
    assert s["by_verdict"]["unknown"] == 1
    assert s["oldest_age_seconds"] == 5 * 3600
    assert summarize_unreconciled([])["count"] == 0
