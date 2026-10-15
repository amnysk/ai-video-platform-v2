"""``application.ports.CommandPort`` の HTTP 側の実装（ADR-0043 §1）。

既存の ``InterventionBackend`` を包み、backend の例外を ``OperationError`` に写す。
"""

from __future__ import annotations

from collections.abc import Sequence

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, ReceivedCommand
from apps.api.auth import ApiPrincipal
from apps.api.intervention_backend import (
    CommandIdReusedError,
    CommandRejectedError,
    EpisodeNotFoundError,
    InterventionBackend,
)
from contracts.intervention import CommandEnvelope


def caller_from_principal(principal: ApiPrincipal) -> Caller:
    return Caller(
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        scopes=principal.scopes,
        principal=principal,
    )


class BackendCommandPort:
    def __init__(self, backend: InterventionBackend) -> None:
        self._backend = backend

    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller
    ) -> list[ReceivedCommand]:
        principal = caller.principal
        assert isinstance(principal, ApiPrincipal)
        try:
            received = await self._backend.receive(episode_id, envelopes, principal)
        except EpisodeNotFoundError:
            raise OperationError(
                OperationErrorCode.EPISODE_NOT_FOUND, f"episode {episode_id} not found"
            ) from None
        except CommandIdReusedError:
            raise OperationError(
                OperationErrorCode.COMMAND_ID_REUSED,
                "this command_id was already used with a different payload; use a new command_id",
            ) from None
        except CommandRejectedError as exc:
            try:
                code = OperationErrorCode(exc.code)
            except ValueError:
                code = OperationErrorCode.INVALID_PAYLOAD
            raise OperationError(
                code, "the command was rejected", {"reject_code": exc.code}
            ) from None
        return [
            ReceivedCommand(r.command.command_id, r.command.status, r.created) for r in received
        ]

    async def start(self, command_ids: Sequence[str]) -> None:
        for command_id in command_ids:
            await self._backend.start_command(command_id)
