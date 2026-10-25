"""ローカル月間予算の純粋な判定（ADR-0061）。I/O をしない。

読むのは設定値だけ。provider へは問い合わせない。未設定と明示的な 0 を区別するが、
どちらも新しい有料 submit は許さない（未設定を無制限と解釈しない）。正の値は「この関門を
通る」だけで、月間の累計・予約の見積・残高の判定は別の関門（Phase 2）が持つ。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from contracts.provider_health import BudgetStatus, HoldReason


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    status: BudgetStatus
    submit_allowed: bool
    #: 保留の理由列の値。予算由来は ``WAITING_FOR_CREDITS``（原因は確定しているので断定してよい）。
    #: ``BUDGET_*`` の区別は ``detail`` とログで持つ（reason の CHECK は広げない / ADR-0061）
    hold_reason: HoldReason | None
    #: ログ・結果に載せる機械判定値（``budget_disabled`` など。secret を含まない）
    decision_reason: str

    @property
    def detail(self) -> dict[str, str]:
        """保留の行に残す機械判定値。"""
        return {"budget_status": self.status.value, "decision_reason": self.decision_reason}


def evaluate_budget(monthly_budget_usd: Decimal | None) -> BudgetDecision:
    if monthly_budget_usd is None:
        return BudgetDecision(
            BudgetStatus.UNSET, False, HoldReason.WAITING_FOR_CREDITS, "budget_unset"
        )
    if not monthly_budget_usd.is_finite() or monthly_budget_usd < 0:
        return BudgetDecision(
            BudgetStatus.UNSET, False, HoldReason.WAITING_FOR_CREDITS, "budget_invalid"
        )
    if monthly_budget_usd == 0:
        return BudgetDecision(
            BudgetStatus.DISABLED, False, HoldReason.WAITING_FOR_CREDITS, "budget_disabled"
        )
    return BudgetDecision(BudgetStatus.ENABLED, True, None, "budget_enabled")
