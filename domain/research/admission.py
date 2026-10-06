"""Research の依頼を外部呼び出しへ進めてよいかの門（ADR-0037 §6）。純粋関数のみ。

Gateway（受け付け・再開）と実行器（開始）が**同じ関数**を使う（AGENTS.md §8）。Gateway を
経由しない依頼作成の経路が増えても、実行器の開始で同じ判定を通る。

- Provider が未設定（``none``）: 外部を呼ばずに ``blocked``（``provider_not_configured``）
- 実 Provider で ``max_cost_usd`` と ``max_youtube_units`` の**どちらか一方でも**未設定:
  外部を呼ばずに ``blocked``（``budget_not_set``）。上限は依頼に凍結されているので、再開しても
  直らない。新しい冪等キーで上限つきの依頼を出し直す
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.research import ResearchLimits, ResearchStopCode

__all__ = ["StopReason", "admission_block", "lacks_budget"]


@dataclass(frozen=True, slots=True)
class StopReason:
    """止めた理由。``blocked_reason`` 列にはこの形（``code`` と ``detail``）で入る。"""

    code: ResearchStopCode
    detail: str

    def as_blocked_reason(self) -> dict[str, Any]:
        return {"code": self.code.value, "detail": self.detail}


def lacks_budget(limits: ResearchLimits, *, provider_is_real: bool) -> bool:
    """実 Provider なのに金額か quota の上限が無い（Fake / 未設定は対象外）。判定はここだけ。"""
    return provider_is_real and (limits.max_cost_usd is None or limits.max_youtube_units is None)


def admission_block(
    limits: ResearchLimits, *, provider_configured: bool, provider_is_real: bool
) -> StopReason | None:
    """外部呼び出しへ進めない理由。進めてよければ ``None``。"""
    if not provider_configured:
        return StopReason(
            ResearchStopCode.PROVIDER_NOT_CONFIGURED,
            "no research provider is configured (RESEARCH_PROVIDER=none); "
            "the owner must choose one (ADR-0036 §3)",
        )
    if lacks_budget(limits, provider_is_real=provider_is_real):
        return StopReason(
            ResearchStopCode.BUDGET_NOT_SET,
            "a real provider needs both max_cost_usd and max_youtube_units (ADR-0037 §6)",
        )
    return None
