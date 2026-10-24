"""予約台帳の費用区分（ADR-0057 / INV-74）。純粋関数。"""

from __future__ import annotations

from decimal import Decimal

from contracts.states import ReservationStatus
from domain.provider.cost import (
    NOT_ACCEPTED_RECONCILER,
    CostState,
    cost_state,
    counted_cost_usd,
)

EST = Decimal("0.0400")


def _state(status, *, dispatched=True, ref=False, raw=False, by=None) -> CostState:
    return cost_state(
        status=status, dispatched=dispatched, has_job_ref=ref, has_raw_output=raw, reconciled_by=by
    )


def test_not_accepted_is_not_charged_and_counts_zero() -> None:
    state = _state(ReservationStatus.SPENT, by=NOT_ACCEPTED_RECONCILER)
    assert state is CostState.NOT_CHARGED
    assert counted_cost_usd(state, EST) == 0


def test_the_five_states_are_distinguished() -> None:
    R, S = ReservationStatus.RESERVED, ReservationStatus.SPENT
    assert _state(R, dispatched=False) is CostState.ESTIMATED
    assert _state(R, ref=True) is CostState.PROVIDER_ACCEPTED
    assert _state(R) is CostState.UNKNOWN  # dispatched・参照なし = 曖昧
    assert _state(S, raw=True, by="evidence") is CostState.CONFIRMED_CHARGED
    assert _state(S, by="conservative") is CostState.ASSUMED_CHARGED


def test_ambiguous_and_accepted_count_at_the_estimate_but_estimated_does_not() -> None:
    assert counted_cost_usd(CostState.UNKNOWN, EST) == EST  # 安全側
    assert counted_cost_usd(CostState.PROVIDER_ACCEPTED, EST) == EST
    assert counted_cost_usd(CostState.ASSUMED_CHARGED, EST) == EST
    assert counted_cost_usd(CostState.ESTIMATED, EST) == 0
    assert counted_cost_usd(CostState.UNKNOWN, None) == 0
