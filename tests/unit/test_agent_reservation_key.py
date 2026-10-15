"""Agent LLM の予約 key は ``provider_reservations.idempotency_key`` の長さに収まる（P1-D の発見）。

SQLite は VARCHAR の長さを検査しないので、実 PostgreSQL の E2E で初めて落ちた。理由:
docs/testing/jpmas2-p1d-rationale.md
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from contracts.agentic_pipeline import agent_stage_idempotency_key
from contracts.agents import resolve_agent_spec, resolve_model_policy
from contracts.intervention import AgentRole
from infrastructure.agents.budget_gate import agent_reservation_key, reserve_and_dispatch_agent_call
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.intervention.budget import BudgetPolicy

SPEC = resolve_agent_spec(AgentRole.STRATEGY, "1.0")
POLICY = resolve_model_policy(SPEC.model_policy_ref)
COLUMN = ProviderReservationRow.__table__.c.idempotency_key.type.length  # type: ignore[attr-defined]


def _stage_key() -> str:
    return agent_stage_idempotency_key(str(uuid.uuid4()), "quality", "a" * 64)


def test_the_stage_key_itself_does_not_fit_the_reservation_column() -> None:
    """前提: Activity の冪等キー（agent_runs 用）は台帳の列より長い。台帳には写して使う。"""
    assert len(_stage_key()) > COLUMN


def test_the_reservation_key_fits_the_column_and_is_deterministic_per_stage_key() -> None:
    key = _stage_key()
    mapped = agent_reservation_key(key)
    assert len(mapped) <= COLUMN
    assert mapped == agent_reservation_key(key)
    assert mapped != agent_reservation_key(key.replace("quality", "creative"))


async def test_a_reservation_for_a_real_sized_stage_key_is_found_again_by_the_mapped_key(
    session,
) -> None:
    episode = await EpisodeRepository(session).create(topic="t")
    await session.commit()
    key = _stage_key()
    for _ in range(2):  # 再送は同じ予約
        reservation = await reserve_and_dispatch_agent_call(
            session,
            episode_id=episode.id,
            idempotency_key=key,
            spec=SPEC,
            policy=POLICY,
            budget_policy=BudgetPolicy(usd_jpy_rate=Decimal("150")),
            now=datetime(2026, 10, 15, tzinfo=UTC),
        )
    assert len(reservation.idempotency_key) <= COLUMN
    found = await ProviderReservationRepository(session).find_by_key(agent_reservation_key(key))
    assert found is not None and found.id == reservation.id
