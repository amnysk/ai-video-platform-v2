"""ユースケースが使う port（ADR-0043 §1）。実装は apps / infrastructure が構造的に満たす。

実装は backend のエラーを ``OperationError`` に写して投げる。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from contracts.intervention import ActorType, CommandEnvelope, CommandStatus, Scope


@dataclass(frozen=True, slots=True)
class Caller:
    actor_type: ActorType
    actor_id: str
    scopes: frozenset[Scope]
    #: port だけが解釈する不透明な値（HTTP では認証済み principal）
    principal: object = None


@dataclass(frozen=True, slots=True)
class ReceivedCommand:
    command_id: str
    status: CommandStatus
    #: False = 同じ command_id・同じ内容の再送
    created: bool
    change_set_id: str | None = None


class CommandPort(Protocol):
    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller
    ) -> list[ReceivedCommand]: ...

    async def start(self, command_ids: Sequence[str]) -> None: ...
