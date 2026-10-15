"""Agent LLM 呼び出しの予算 gate と予約台帳（ADR-0043 §2、ADR-0013、INV-15/44/46/56）。

既存の Codex LLM 呼び出し（``workers/planning/activities.py``）と同じ順序をたどる:

1. 別の key の evidence の無い予約が残っていれば新しい呼び出しを始めない
   （``UnreconciledReservationError``）
2. **その key の最初の予約の前に**だけ ``budget_view`` + ``check_additional_cost`` を検査する。
   拒否は ``AgentBudgetRefusedError``（予約は作らない）。再送（予約が既にある）は再検査しない
3. ``ProviderCall.AGENT_LLM`` を ``estimated_cost_usd = spec.max_estimated_cost_usd``
   （上限）で予約して **commit**（INV-15: 予約が先）。上限で数えるので、実績が測れなくても予算に載る
4. ``mark_dispatched``（内部で dispatch gate ``check_dispatch_allowed``。hold 中は
   ``EpisodeHeldError``、予約は未 dispatch のまま残る）を commit

返す予約の ``raw_output_key`` が入っていれば、前回の試行が呼び終えている。**再送しない**こと。
呼び出し後は ``settle_agent_call`` で「呼んだ」事実を確定する（検証より前）。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from contracts.agents import AgentSpec, ModelPolicy
from contracts.states import ProviderCall, ReservationStatus
from domain.errors import (
    AgentBudgetRefusedError,
    UnreconciledReservationError,
    classify_failure,
)
from infrastructure.db.repositories import ProviderReservation, ProviderReservationRepository
from infrastructure.intervention.budget import BudgetPolicy, budget_view, check_additional_cost

#: 台帳の ``input_hash`` に入れる値の元（冪等キーから決定的に導く）
_ROUND = 1


async def reserve_and_dispatch_agent_call(
    session: AsyncSession,
    *,
    episode_id: uuid.UUID | str,
    idempotency_key: str,
    spec: AgentSpec,
    policy: ModelPolicy,
    budget_policy: BudgetPolicy,
    now: datetime,
) -> ProviderReservation:
    if spec.model_policy_ref != policy.ref:
        raise ValueError(f"spec pins {spec.model_policy_ref} but got policy {policy.ref}")
    repo = ProviderReservationRepository(session)
    reservation = await repo.find_by_key(idempotency_key)
    if reservation is None:
        stale = [
            row
            for row in await repo.find_unreconciled(
                episode_id=episode_id, provider=ProviderCall.AGENT_LLM
            )
            if row.idempotency_key != idempotency_key
        ]
        if stale:
            raise UnreconciledReservationError(
                f"unreconciled reservation {stale[0].id} blocks a new agent LLM call"
            )
        view = await budget_view(session, episode_id, budget_policy, now)
        reason = check_additional_cost(view, spec.max_estimated_cost_usd)
        if reason is not None:
            raise AgentBudgetRefusedError(
                f"agent {spec.role.value}@{spec.spec_version} refused: {reason.value} "
                f"(ceiling {spec.max_estimated_cost_usd} USD)",
                reason=reason.value,
            )
        reservation = await repo.reserve(
            episode_id=episode_id,
            provider=ProviderCall.AGENT_LLM,
            idempotency_key=idempotency_key,
            input_hash=hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest(),
            round=_ROUND,
            estimated_cost_usd=spec.max_estimated_cost_usd,
        )
        await session.commit()  # INV-15: 予約を commit してから dispatch へ進む
    if reservation.status is ReservationStatus.RESERVED and reservation.dispatched_at is None:
        reservation = await repo.mark_dispatched(reservation.id)
        await session.commit()
    return reservation


async def settle_agent_call(
    session: AsyncSession,
    reservation_id: uuid.UUID | str,
    *,
    raw_output_key: str | None = None,
    error: BaseException | None = None,
) -> ProviderReservation:
    """呼び出しの結果を台帳に確定して commit する。**出力の検証より前**に呼ぶ。

    - 出力あり: ``raw_output_key`` が evidence（``reconciled_by="evidence"``）
    - 呼び出しが戻ってきた上で失敗: 課金された前提で保守的に確定（``conservative``）。
      未照合のまま残すと次の呼び出しが永久にブロックされる（ADR-0013）
    """
    repo = ProviderReservationRepository(session)
    if error is not None:
        settled = await repo.mark_spent(
            reservation_id,
            raw_output_key=None,
            reconciled_by="conservative",
            failure_class=classify_failure(error),
            error_summary=f"{type(error).__name__}: {error}"[:500],
        )
    else:
        if raw_output_key is None:
            raise ValueError("a successful call needs raw_output_key as evidence")
        settled = await repo.mark_spent(
            reservation_id, raw_output_key=raw_output_key, reconciled_by="evidence"
        )
    await session.commit()
    return settled
