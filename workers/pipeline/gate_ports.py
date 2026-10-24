"""日次の入口ゲート・production ゲートが使う port の束と、その結線点（ADR-0058）。

ゲートは ``contracts.provider_health`` の Protocol にだけ依存する。実装（breaker・保留の表、
fal 障害耐性 = ADR-0057）はここの ``build_gate_ports`` が返す束に差し込む。

**結線の唯一の場所は ``build_gate_ports``**。結線前（port が ``None``）は「breaker は closed・
保留 0」として動く（従来どおり）。この場合は起動時に警告を出す（保留・breaker が効いていないことを
黙らせない）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.daily_gate import OpenHoldReader
from contracts.provider_health import CreditHoldWriter, ProviderHealthReader
from infrastructure.db.repositories import ProviderReconciliationRepository


class UnreconciledCounter(Protocol):
    async def count(self) -> int: ...


class DbUnreconciledCounter:
    """``status = reserved`` の予約数（release preflight の未照合件数と同じ集合 / ADR-0053）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def count(self) -> int:
        async with self._session_factory() as session:
            return len(await ProviderReconciliationRepository(session).list_unreconciled())


@dataclass
class GatePorts:
    health: ProviderHealthReader | None = None
    holds: CreditHoldWriter | None = None
    open_holds: OpenHoldReader | None = None
    unreconciled: UnreconciledCounter | None = None

    @property
    def wired(self) -> bool:
        return self.health is not None and self.holds is not None


def build_gate_ports(session_factory: async_sessionmaker[AsyncSession]) -> GatePorts:
    """pipeline worker が使う port を組む。**ここが ADR-0057 の実装との結線点**。

    統合では ``health`` / ``holds`` / ``open_holds`` を保留テーブルの実装に差し替える。
    """
    return GatePorts(unreconciled=DbUnreconciledCounter(session_factory))
