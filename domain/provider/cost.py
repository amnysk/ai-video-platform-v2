"""予約台帳の費用の区分（ADR-0057 / INV-74）。純粋関数のみ。

``provider_reservations.estimated_cost_usd`` は**見積もり**であって請求額ではない。
同じ見積額でも、課金されたかの確からしさは行の状態で違う。区分を行から導く（列を足さない）:

| 区分 | 意味 | 費用として数える額 |
|---|---|---|
| ``estimated`` | 予約しただけ。まだ provider を呼んでいない | 0 |
| ``provider_accepted`` | provider が受理した（job 参照あり）。完了は未確認 | 見積額 |
| ``confirmed_charged`` | provider が成果物を返した（生出力あり） | 見積額 |
| ``not_charged`` | **受理されなかったことが確実**（残高不足・認可拒否など） | 0 |
| ``assumed_charged`` | 戻った失敗を課金前提で確定（旧 conservative）。過大計上あり | 見積額 |
| ``unknown`` | 呼んだ可能性があるが結果が未観測（曖昧）。**自動で再送しない** | 見積額（安全側） |
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from contracts.states import ReservationStatus

#: 受理されなかったことが確実な拒否で確定した予約の ``reconciled_by``。
#: ``reconciled_by`` は自由文字列で CHECK 制約が無い（既存の制約は広げない。ADR-0057）。
NOT_ACCEPTED_RECONCILER = "not_accepted"


class CostState(StrEnum):
    ESTIMATED = "estimated"
    PROVIDER_ACCEPTED = "provider_accepted"
    CONFIRMED_CHARGED = "confirmed_charged"
    NOT_CHARGED = "not_charged"
    ASSUMED_CHARGED = "assumed_charged"
    UNKNOWN = "unknown"


#: 費用として数えない区分。これ以外は見積額を数える（安全側）。
UNCOUNTED_COST_STATES = frozenset({CostState.ESTIMATED, CostState.NOT_CHARGED})


#: 月間ハードキャップ（ADR-0063）で数えない区分。``UNCOUNTED_COST_STATES`` より狭い:
#: 未 dispatch の ``reserved``（``ESTIMATED``）は「これから使う枠」なので上限では数える。
#: **受理されなかったことが確実**な行だけ解放する。
CAP_UNCOUNTED_COST_STATES = frozenset({CostState.NOT_CHARGED})


def cost_state(
    *,
    status: ReservationStatus,
    dispatched: bool,
    has_job_ref: bool,
    has_raw_output: bool,
    reconciled_by: str | None,
) -> CostState:
    if reconciled_by == NOT_ACCEPTED_RECONCILER:
        return CostState.NOT_CHARGED
    if status is ReservationStatus.SPENT:
        return CostState.CONFIRMED_CHARGED if has_raw_output else CostState.ASSUMED_CHARGED
    if status is ReservationStatus.RESERVED:
        if not dispatched:
            return CostState.ESTIMATED
        return CostState.PROVIDER_ACCEPTED if has_job_ref else CostState.UNKNOWN
    return CostState.UNKNOWN  # abandoned: 人手の結論だが、費用の有無は台帳だけでは言えない


def counted_cost_usd(state: CostState, estimated: Decimal | None) -> Decimal:
    """この区分の予約が費用として数える額。"""
    if estimated is None or state in UNCOUNTED_COST_STATES:
        return Decimal("0")
    return estimated


def cap_counted_cost_usd(state: CostState, estimated: Decimal | None) -> Decimal | None:
    """月間ハードキャップで数える額。``None`` は「数えるのに見積が無い」（= 不明。拒否側）。

    SQL 側の集計（``infrastructure.db`` の ``month_counted``）はこの定義を SQL に写したもの。
    """
    if state in CAP_UNCOUNTED_COST_STATES:
        return Decimal("0")
    return estimated


__all__ = [
    "CAP_UNCOUNTED_COST_STATES",
    "NOT_ACCEPTED_RECONCILER",
    "UNCOUNTED_COST_STATES",
    "CostState",
    "cap_counted_cost_usd",
    "cost_state",
    "counted_cost_usd",
]
