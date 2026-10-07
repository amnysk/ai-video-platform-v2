"""介入の予算の読み取りと検査（``docs/domain/intervention.md`` §9、設計 §2）。

上限は ``EPISODE_BUDGET_JPY``（既定 1000）・``MONTHLY_BUDGET_JPY``（既定 30000）。USD→JPY の換算
``BUDGET_USD_JPY_RATE`` は既定なし。換算が未設定なら有料の見積りを伴う変更は検証できない
（``budget_unverifiable``）。

支出の正本は予約台帳（``provider_reservations.estimated_cost_usd`` の reserved + spent）と
``research_calls``（reserved + spent。Episode に結び付くものは ``research_requests.episode_id``）。
LLM（Codex）の費用は記録が無いので ``unmeasured`` として別に示し、0 として足さない。

**既存の日次経路に予算の強制は無い**。ここは新方式の ChangeSet 適用時の検査だけ（ADR-0041 の負債）。
上限を変える経路は Settings（所有者）だけで、介入 API・AI からは変えられない（INV-47）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.intervention import (
    DEFAULT_EPISODE_BUDGET_JPY,
    DEFAULT_MONTHLY_BUDGET_JPY,
    RejectReason,
)
from contracts.pipeline import DEFAULT_SCHEDULE_TIMEZONE
from contracts.research import ResearchCallStatus
from contracts.states import ReservationStatus
from infrastructure.db.models import ProviderReservationRow, ResearchCallRow, ResearchRequestRow

#: LLM の費用の表示（記録が無い。0 として扱わない）
UNMEASURED = "unmeasured"

_COUNTED_RESERVATIONS = (ReservationStatus.RESERVED.value, ReservationStatus.SPENT.value)
_COUNTED_RESEARCH = (ResearchCallStatus.RESERVED.value, ResearchCallStatus.SPENT.value)


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    episode_budget_jpy: int = DEFAULT_EPISODE_BUDGET_JPY
    monthly_budget_jpy: int = DEFAULT_MONTHLY_BUDGET_JPY
    #: 1 USD あたりの円。None なら有料の変更は検証できない
    usd_jpy_rate: Decimal | None = None
    #: 「今月」の境界を決めるタイムゾーン（日次 Schedule と同じ既定）
    timezone: str = DEFAULT_SCHEDULE_TIMEZONE


@dataclass(frozen=True, slots=True)
class BudgetView:
    episode_spent_usd: Decimal
    monthly_spent_usd: Decimal
    #: 見積りの無い予約（Codex の script / storyboard 等）の件数。費用は unmeasured
    episode_unpriced_calls: int
    episode_budget_jpy: int
    monthly_budget_jpy: int
    usd_jpy_rate: Decimal | None
    llm_cost: str = UNMEASURED

    @property
    def verifiable(self) -> bool:
        return self.usd_jpy_rate is not None

    def to_jpy(self, usd: Decimal) -> Decimal | None:
        return None if self.usd_jpy_rate is None else usd * self.usd_jpy_rate

    @property
    def episode_remaining_jpy(self) -> Decimal | None:
        spent = self.to_jpy(self.episode_spent_usd)
        return None if spent is None else Decimal(self.episode_budget_jpy) - spent

    @property
    def monthly_remaining_jpy(self) -> Decimal | None:
        spent = self.to_jpy(self.monthly_spent_usd)
        return None if spent is None else Decimal(self.monthly_budget_jpy) - spent

    def as_dict(self) -> dict[str, object]:
        def _s(value: Decimal | None) -> str | None:
            return None if value is None else str(value.quantize(Decimal("0.0001")))

        return {
            "episode_spent_usd": _s(self.episode_spent_usd),
            "monthly_spent_usd": _s(self.monthly_spent_usd),
            "episode_unpriced_calls": self.episode_unpriced_calls,
            "episode_budget_jpy": self.episode_budget_jpy,
            "monthly_budget_jpy": self.monthly_budget_jpy,
            "usd_jpy_rate": None if self.usd_jpy_rate is None else str(self.usd_jpy_rate),
            "episode_remaining_jpy": _s(self.episode_remaining_jpy),
            "monthly_remaining_jpy": _s(self.monthly_remaining_jpy),
            "llm_cost": self.llm_cost,
        }


def _month_start(now: datetime, timezone: str) -> datetime:
    local = now.astimezone(ZoneInfo(timezone))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def budget_view(
    session: AsyncSession, episode_id: uuid.UUID | str, policy: BudgetPolicy, now: datetime
) -> BudgetView:
    """Episode と今月の支出（見積り USD）を予約台帳と research の台帳から数える。"""
    ep = _uuid(episode_id)
    r = ProviderReservationRow
    episode_paid = await session.scalar(
        select(func.coalesce(func.sum(r.estimated_cost_usd), 0)).where(
            r.episode_id == ep, r.status.in_(_COUNTED_RESERVATIONS)
        )
    )
    unpriced = await session.scalar(
        select(func.count()).where(
            r.episode_id == ep,
            r.status.in_(_COUNTED_RESERVATIONS),
            r.estimated_cost_usd.is_(None),
        )
    )
    c, q = ResearchCallRow, ResearchRequestRow
    episode_research = await session.scalar(
        select(func.coalesce(func.sum(c.estimated_cost_usd), 0))
        .join(q, q.id == c.request_id)
        .where(q.episode_id == ep, c.status.in_(_COUNTED_RESEARCH))
    )
    since = _month_start(now, policy.timezone)
    monthly_paid = await session.scalar(
        select(func.coalesce(func.sum(r.estimated_cost_usd), 0)).where(
            r.status.in_(_COUNTED_RESERVATIONS), r.reserved_at >= since
        )
    )
    monthly_research = await session.scalar(
        select(func.coalesce(func.sum(c.estimated_cost_usd), 0)).where(
            c.status.in_(_COUNTED_RESEARCH), c.reserved_at >= since
        )
    )
    return BudgetView(
        episode_spent_usd=Decimal(str(episode_paid or 0)) + Decimal(str(episode_research or 0)),
        monthly_spent_usd=Decimal(str(monthly_paid or 0)) + Decimal(str(monthly_research or 0)),
        episode_unpriced_calls=int(unpriced or 0),
        episode_budget_jpy=policy.episode_budget_jpy,
        monthly_budget_jpy=policy.monthly_budget_jpy,
        usd_jpy_rate=policy.usd_jpy_rate,
    )


def check_additional_cost(view: BudgetView, estimated_usd: Decimal) -> RejectReason | None:
    """追加の見積り費用がこの Episode・今月の残予算に収まるか。収まらなければ理由。

    0 円の変更（ローカル TTS・render だけ）は換算が無くても通す。
    """
    if estimated_usd <= 0:
        return None
    if view.usd_jpy_rate is None:
        return RejectReason.BUDGET_UNVERIFIABLE
    extra = estimated_usd * view.usd_jpy_rate
    episode_left = view.episode_remaining_jpy
    monthly_left = view.monthly_remaining_jpy
    assert episode_left is not None and monthly_left is not None
    if extra > episode_left or extra > monthly_left:
        return RejectReason.BUDGET_EXCEEDED
    return None
