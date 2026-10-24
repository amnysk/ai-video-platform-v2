"""復旧上限への投影は、受理されなかった拒否の直後でも見積り額を使う（review-E P2-7）。

``list_paid_reservation_costs`` は not_accepted の拒否を支出 0 にする（INV-74）。しかし「次の
作り直し」の投影に最新の予約の額を使うと、最新が拒否だと 0 になり、残高が戻って実際に再生成
するときの費用が上限判定から消える。支出は 0 のまま、投影だけが見積り額を使う。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from contracts.states import ProviderCall
from infrastructure.db.repositories import (
    PaidReservationCost,
)
from workers.production.scene_recovery_activities import _projected_rebuild_cost

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _cost(provider: ProviderCall, *, spent: str, nominal: str, minutes: int) -> PaidReservationCost:
    return PaidReservationCost(
        scene_id="s1",
        provider=provider,
        estimated_cost_usd=Decimal(spent),
        nominal_cost_usd=Decimal(nominal),
        input_rejected_by_provider=False,
        reserved_at=T0 + timedelta(minutes=minutes),
    )


def test_projection_uses_the_estimate_when_the_latest_round_was_not_accepted() -> None:
    costs = [
        _cost(ProviderCall.FAL_IMAGE, spent="0.04", nominal="0.04", minutes=0),
        _cost(ProviderCall.FAL_IMAGE, spent="0", nominal="0.04", minutes=5),  # 拒否
        _cost(ProviderCall.FAL_VIDEO, spent="0", nominal="0.50", minutes=6),  # 拒否
    ]
    assert _projected_rebuild_cost(costs, "s1") == Decimal("0.54")
