"""``application.ports.CommandPort`` の HTTP 側の実装（ADR-0043 §1）。

既存の ``InterventionBackend`` を包み、backend の例外を ``OperationError`` に写す。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from application.errors import OperationError, OperationErrorCode
from application.ports import Caller, OperationRecord, ReceivedCommand
from apps.api.auth import ApiPrincipal
from apps.api.intervention_backend import (
    CommandIdReusedError,
    CommandRejectedError,
    EpisodeNotFoundError,
    InterventionBackend,
)
from contracts.intervention import CommandEnvelope
from contracts.log_contract import EventName, Outcome
from infrastructure.logging.emit import emit, log_guard

logger = logging.getLogger("avp.api.application")


def caller_from_principal(principal: ApiPrincipal, correlation_id: str | None = None) -> Caller:
    return Caller(
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        scopes=principal.scopes,
        principal=principal,
        correlation_id=correlation_id,
    )


class LoggingOperationObserver:
    """``OperationObserver`` の HTTP 側の実装: 1 操作 = 構造化ログ 1 行（ADR-0044 §5）。

    token・入力全文は ``OperationRecord`` が持たないので載せようがない。ログの故障は業務へ伝えない。
    """

    def record(self, record: OperationRecord) -> None:
        with log_guard():
            emit(
                logger,
                EventName.APPLICATION_OPERATION_COMPLETED,
                logging.ERROR if record.result == Outcome.FAILED.value else logging.INFO,
                "application operation %s %s",
                record.operation.value,
                record.result,
                episode_id=record.episode_id,
                correlation_id=record.correlation_id,
                command_id=record.command_id,
                actor_type=record.actor_type.value,
                workflow_id=record.workflow_id,
                outcome=record.result,
                duration_ms=record.duration_ms,
                error_code=record.error_code,
                operation=record.operation.value,
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
