"""日次ゲート・watchdog の port が ADR-0057 の実装に結線されている（INV-76 / INV-77）。

理由は docs/testing/provider-health-rationale.md「結線」。
"""

from __future__ import annotations

import dataclasses

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from contracts.daily_gate import OpenHold
from infrastructure.db.provider_health_repositories import PostgresProviderHealth
from workers.pipeline.gate_ports import build_gate_ports


def _factory() -> async_sessionmaker:
    return async_sessionmaker(create_async_engine("sqlite+aiosqlite://"))


def test_production_ports_are_wired_to_the_breaker_and_hold_tables() -> None:
    ports = build_gate_ports(_factory())
    assert ports.wired
    assert isinstance(ports.health, PostgresProviderHealth)
    assert isinstance(ports.holds, PostgresProviderHealth)
    # watchdog は open_holds で保留を除外する。未結線だと保留中の Episode を stall と誤報する
    assert isinstance(ports.open_holds, PostgresProviderHealth)
    assert ports.unreconciled is not None


def test_open_holds_return_the_single_contract_type() -> None:
    # 同じ5フィールドの複製型を持たない（AGENTS.md §8）
    import infrastructure.db.provider_health_repositories as repo

    assert not hasattr(repo, "OpenHoldRecord")
    hints = PostgresProviderHealth.list_open_holds.__annotations__["return"]
    assert "OpenHold" in str(hints)
    assert {f.name for f in dataclasses.fields(OpenHold)} == {
        "episode_id",
        "held_since",
        "reason",
        "account_key",
        "resumable_stage",
    }
