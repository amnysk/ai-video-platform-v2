"""LLM 費用の計算（ADR-0043 §2）。**欠測を 0 にしない**: 不明は ``None``。"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from contracts.agents import ModelPolicy

_PER = Decimal(1000)


def llm_call_cost(
    policy: ModelPolicy, input_tokens: int | None, output_tokens: int | None
) -> Decimal | None:
    """1 回の呼び出しの費用（USD）。token 数か単価のどれかが欠ければ ``None``。"""
    if (
        input_tokens is None
        or output_tokens is None
        or policy.input_usd_per_1k_tokens is None
        or policy.output_usd_per_1k_tokens is None
    ):
        return None
    return (
        Decimal(input_tokens) * policy.input_usd_per_1k_tokens
        + Decimal(output_tokens) * policy.output_usd_per_1k_tokens
    ) / _PER


def sum_costs(costs: Iterable[Decimal | None]) -> Decimal | None:
    """合計。1 件でも ``None`` なら ``None``（測れた分だけの合計を実績として出さない）。"""
    total = Decimal(0)
    for cost in costs:
        if cost is None:
            return None
        total += cost
    return total
