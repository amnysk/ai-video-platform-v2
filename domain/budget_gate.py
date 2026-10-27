"""ローカル月間予算の純粋な判定（ADR-0061）。I/O をしない。

読むのは設定値だけ。provider へは問い合わせない。未設定と明示的な 0 を区別するが、
どちらも新しい有料 submit は許さない（未設定を無制限と解釈しない）。正の値は「この関門を
通る」だけで、月間の累計・予約の見積・残高の判定は別の関門（Phase 2）が持つ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
    #: 月間のハードキャップ（ADR-0063）。``ENABLED`` のときだけ入る（それ以外は ``None``）
    cap_usd: Decimal | None = None

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
    return BudgetDecision(
        BudgetStatus.ENABLED, True, None, "budget_enabled", cap_usd=monthly_budget_usd
    )


# --------------------------------------------------------------- 月間ハードキャップ（ADR-0063）

CAP_WITHIN = "within_cap"
CAP_EXCEEDED = "monthly_cap_exceeded"
CAP_INVALID_INPUT = "invalid_input"
CAP_UNKNOWN_COST = "unknown_cost"

_ZERO = Decimal("0")


def month_start_utc(now: datetime) -> datetime:
    """``now`` の属する UTC 月の月初（naive UTC。``reserved_at`` と同じ形）。

    月間の集計の境界はここ 1 箇所（status / resume / 上限検査が共有する）。naive の ``now`` は
    UTC として扱う。
    """
    moment = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def month_key(now: datetime) -> str:
    """``YYYY-MM``（UTC）。月ロックの鍵の材料。"""
    return month_start_utc(now).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class CapDecision:
    allowed: bool
    #: ``within_cap`` / ``monthly_cap_exceeded`` / ``invalid_input`` / ``unknown_cost``
    reason: str
    month_counted: Decimal
    estimate: Decimal
    cap: Decimal
    remaining: Decimal


def _usable(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def evaluate_cap(cap: Decimal, month_counted: Decimal, estimate: Decimal) -> CapDecision:
    """新しい有料予約が月間の上限内か。``month_counted + estimate <= cap`` のときだけ許可。

    ちょうど等しいなら許可、超えるなら拒否。負・非有限の入力は拒否（fail closed）。
    """
    if not (_usable(cap) and _usable(month_counted) and _usable(estimate)):
        return CapDecision(
            False, CAP_INVALID_INPUT, _safe(month_counted), _safe(estimate), _safe(cap), _ZERO
        )
    remaining = max(cap - month_counted, _ZERO)
    if month_counted + estimate <= cap:
        return CapDecision(True, CAP_WITHIN, month_counted, estimate, cap, remaining)
    return CapDecision(False, CAP_EXCEEDED, month_counted, estimate, cap, remaining)


def unknown_cost_decision(
    cap: Decimal, month_counted: Decimal, estimate: Decimal | None
) -> CapDecision:
    """見積が出せない / 数える行に見積 NULL がある。費用が分からないので拒否（fail closed）。"""
    return CapDecision(
        False,
        CAP_UNKNOWN_COST,
        _safe(month_counted),
        _safe(estimate),
        _safe(cap),
        max(_safe(cap) - _safe(month_counted), _ZERO),
    )


def _safe(value: Decimal | None) -> Decimal:
    """診断用の表示値。非有限・None は 0 に寄せる（判定には使わない）。"""
    if isinstance(value, Decimal) and value.is_finite():
        return value
    return _ZERO
