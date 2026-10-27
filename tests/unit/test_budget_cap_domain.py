"""月間 hard cap の純粋判定（INV-86 / ADR-0063）。I/O なし・Decimal のみ。

理由は docs/testing/fal-monthly-cap-rationale.md。
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from contracts.provider_health import BudgetStatus
from contracts.states import FailureClass
from domain.budget_gate import CapDecision, evaluate_budget, evaluate_cap
from domain.errors import (
    FAILURE_CLASS_BY_TYPE_NAME,
    ProviderBreakerOpenError,
    ProviderBudgetHeldError,
    ProviderMonthlyBudgetExceededError,
    failure_class_from_type_name,
)

D = Decimal


def test_within_cap_is_allowed_and_reports_remaining_before_the_new_estimate() -> None:
    d = evaluate_cap(D("60"), D("10"), D("5"))
    assert d == CapDecision(True, "within_cap", D("10"), D("5"), D("60"), D("50"))


def test_exactly_at_the_cap_is_allowed() -> None:
    d = evaluate_cap(D("60"), D("55"), D("5"))
    assert d.allowed is True and d.reason == "within_cap"


def test_one_unit_over_the_cap_is_denied() -> None:
    d = evaluate_cap(D("60"), D("55"), D("5.0001"))
    assert d.allowed is False and d.reason == "monthly_cap_exceeded"
    assert d.remaining == D("5")  # 拒否でも「あと何ドル残っているか」は正しく出す


def test_already_over_the_cap_denies_even_a_zero_estimate_and_clamps_remaining() -> None:
    d = evaluate_cap(D("60"), D("70"), D("0"))
    assert d.allowed is False and d.reason == "monthly_cap_exceeded"
    assert d.remaining == D("0")  # max(cap - counted, 0)。負にならない


def test_empty_month_single_estimate_equal_to_cap_is_allowed() -> None:
    assert evaluate_cap(D("60"), D("0"), D("60")).allowed is True
    assert evaluate_cap(D("60"), D("0"), D("60.01")).allowed is False


def test_decimal_arithmetic_is_exact_not_float() -> None:
    # float なら 0.1 + 0.2 > 0.3。Decimal は厳密に等しい（= 許可）
    assert evaluate_cap(D("0.3"), D("0.1"), D("0.2")).allowed is True
    assert evaluate_cap(D("0.3"), D("0.1"), D("0.2000001")).allowed is False


def test_values_keep_their_decimal_type_and_precision() -> None:
    d = evaluate_cap(D("60.0000"), D("12.3456"), D("0.0001"))
    for value in (d.month_counted, d.estimate, d.cap, d.remaining):
        assert isinstance(value, Decimal)
    assert d.remaining == D("47.6544")


@pytest.mark.parametrize(
    ("cap", "counted", "estimate"),
    [
        (D("60"), D("-1"), D("1")),
        (D("60"), D("1"), D("-0.0001")),
        (D("-60"), D("0"), D("1")),
        (D("NaN"), D("0"), D("1")),
        (D("60"), D("NaN"), D("1")),
        (D("60"), D("0"), D("NaN")),
        (D("Infinity"), D("0"), D("1")),  # 無限大の cap は「cap 無し」ではなく不正値
        (D("60"), D("Infinity"), D("1")),
        (D("60"), D("0"), D("Infinity")),
        (D("60"), D("0"), D("-Infinity")),
    ],
)
def test_negative_or_non_finite_inputs_fail_closed(
    cap: Decimal, counted: Decimal, estimate: Decimal
):
    d = evaluate_cap(cap, counted, estimate)
    assert d.allowed is False
    assert d.reason == "invalid_input"


def test_the_decision_is_immutable() -> None:
    d = evaluate_cap(D("60"), D("0"), D("1"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        d.allowed = False  # type: ignore[misc]


def test_budget_decision_carries_the_cap_only_when_enabled() -> None:
    assert evaluate_budget(D("60")).cap_usd == D("60")
    assert evaluate_budget(D("60")).status is BudgetStatus.ENABLED
    for value in (None, D("0"), D("-1"), D("NaN"), D("Infinity")):
        decision = evaluate_budget(value)
        assert decision.cap_usd is None
        assert decision.submit_allowed is False  # 既存の on/off の意味は変わらない


def test_the_exceeded_error_is_a_budget_hold_and_therefore_needs_input() -> None:
    """クラッシュではなく既存の保留経路（NEEDS_INPUT）へ流れる型であること。"""
    assert issubclass(ProviderMonthlyBudgetExceededError, ProviderBudgetHeldError)
    assert issubclass(ProviderMonthlyBudgetExceededError, ProviderBreakerOpenError)
    name = ProviderMonthlyBudgetExceededError.__name__
    assert failure_class_from_type_name(name) is FailureClass.NEEDS_INPUT
    assert (
        FAILURE_CLASS_BY_TYPE_NAME.get(name, FailureClass.NEEDS_INPUT) is FailureClass.NEEDS_INPUT
    )
