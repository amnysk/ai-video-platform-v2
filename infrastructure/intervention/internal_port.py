"""内部（Agent）用の ``CommandPort``（ADR-0043 §1）。HTTP を経由せず ``receive_command`` を呼ぶ。

- 自前の session を開き、全 envelope を**1 transaction**で受け付けて commit する（HTTP と同じ）。
- 主体は ``agent`` か ``ai_assistant`` だけ（人・system にしない）。scope は呼び出し側が渡した
  もの（Agent は ``read`` / ``comment`` / ``propose`` だけを持たせる。apply は与えない）。
- ``application`` は import しない（INV-6）。``application.ports.CommandPort`` を構造的に満たす。
  失敗は ``contracts.application.OperationError`` で投げる。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from contracts.application import OperationError, OperationErrorCode
from contracts.intervention import ActorType, CommandEnvelope, CommandStatus, Scope
from infrastructure.intervention import commands
from infrastructure.intervention.store import Actor, DuplicateCommandError

logger = logging.getLogger("avp.intervention.internal_port")

#: この port が受け付ける主体。人の操作として記録しない（AI の提案を人の直接編集と誤記録しない）
INTERNAL_ACTOR_TYPES: frozenset[ActorType] = frozenset({ActorType.AGENT, ActorType.AI_ASSISTANT})


class CommandStarter(Protocol):
    async def start(self, command_id: str) -> None: ...


class _CallerLike(Protocol):
    @property
    def actor_type(self) -> ActorType: ...
    @property
    def actor_id(self) -> str: ...
    @property
    def scopes(self) -> frozenset[Scope]: ...


@dataclass(frozen=True, slots=True)
class _Received:
    command_id: str
    status: CommandStatus
    created: bool
    change_set_id: str | None = None


class SessionCommandPort:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], starter: CommandStarter
    ) -> None:
        self._session_factory = session_factory
        self._starter = starter

    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Any
    ) -> list[Any]:
        who: _CallerLike = caller
        if who.actor_type not in INTERNAL_ACTOR_TYPES:
            raise OperationError(
                OperationErrorCode.FORBIDDEN,
                "the internal port accepts agent / ai_assistant callers only",
            )
        actor = Actor(who.actor_type, who.actor_id)
        principal = commands.Principal(actor=actor, scopes=frozenset(who.scopes))
        async with self._session_factory() as session:
            out: list[_Received] = []
            try:
                for envelope in envelopes:
                    result = await commands.receive_command(
                        session, episode_id, envelope, principal
                    )
                    if not result.created and result.command.actor != actor:
                        # 別の主体の command を自分の再送として受け取らない
                        raise DuplicateCommandError(result.command)
                    out.append(
                        _Received(result.command.command_id, result.command.status, result.created)
                    )
            except DuplicateCommandError:
                await session.rollback()
                raise OperationError(
                    OperationErrorCode.COMMAND_ID_REUSED,
                    "this command_id was already used with a different payload; "
                    "use a new command_id",
                ) from None
            except commands.EpisodeNotFoundError:
                await session.rollback()
                raise OperationError(
                    OperationErrorCode.EPISODE_NOT_FOUND, f"episode {episode_id} not found"
                ) from None
            except commands.CommandForbiddenError:
                await session.rollback()
                raise OperationError(
                    OperationErrorCode.FORBIDDEN, "the command was rejected"
                ) from None
            except commands.CommandReceiveError as exc:
                await session.rollback()
                raise OperationError(
                    OperationErrorCode.INVALID_PAYLOAD,
                    "the command was rejected",
                    {"reject_code": exc.code},
                ) from None
            await session.commit()
            return list(out)

    async def start(self, command_ids: Sequence[str]) -> None:
        """best-effort（commit 済み。失敗は再配送 Schedule が拾う）。"""
        for command_id in command_ids:
            try:
                await self._starter.start(command_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "command workflow start failed; redelivery will retry "
                    "(command_id=%s, error=%s)",
                    command_id,
                    type(exc).__name__,
                )
