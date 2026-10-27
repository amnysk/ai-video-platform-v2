"""ローカル月間予算の純粋判定（ADR-0061）。未設定と明示的な 0 を区別する。"""

from __future__ import annotations

from decimal import Decimal

import pytest

from contracts.provider_health import BudgetStatus, HoldReason
from domain.budget_gate import evaluate_budget


def test_explicit_zero_is_disabled_and_holds_without_any_provider_call() -> None:
    decision = evaluate_budget(Decimal("0"))
    assert decision.status is BudgetStatus.DISABLED
    assert decision.submit_allowed is False
    assert decision.hold_reason is HoldReason.WAITING_FOR_CREDITS


def test_unset_is_not_unlimited() -> None:
    decision = evaluate_budget(None)
    assert decision.status is BudgetStatus.UNSET
    assert decision.submit_allowed is False
    assert decision.hold_reason is HoldReason.WAITING_FOR_CREDITS


@pytest.mark.parametrize("value", [Decimal("-1"), Decimal("NaN"), Decimal("Infinity")])
def test_invalid_values_fail_closed_as_unset(value: Decimal) -> None:
    decision = evaluate_budget(value)
    assert decision.status is BudgetStatus.UNSET
    assert decision.submit_allowed is False
    assert decision.hold_reason is HoldReason.WAITING_FOR_CREDITS


def test_positive_budget_passes_this_gate_only() -> None:
    decision = evaluate_budget(Decimal("25.50"))
    assert decision.status is BudgetStatus.ENABLED
    assert decision.submit_allowed is True
    assert decision.hold_reason is None


def test_decision_reason_is_machine_readable_and_secret_free() -> None:
    for value in (None, Decimal("0"), Decimal("5")):
        assert evaluate_budget(value).decision_reason.startswith("budget_")
