"""``/v1/intervention`` の1要求を ``intervention.api.request`` として記録する（ADR-0042 §6）。

pure ASGI middleware。出すのは route の template・主体の種別と ID・status・所要時間・相関 ID・
assistant の tool 名・episode_id だけ。**Authorization・token・query・本文は読まない／出さない**
（INV-39）。相関 ID と tool 名は安全な文字だけのときに限り記録する（ログ注入を避ける）。
正本は command 行と ``episode_events``。これは副本の検索用（INV-38: 故障は応答を変えない）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from apps.api.auth import AUTH_OBSERVER
from contracts.log_contract import EventName, Outcome
from infrastructure.logging.emit import emit, log_guard

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("avp.api.intervention")

INTERVENTION_PATH_PREFIX = "/v1/intervention"
CORRELATION_HEADER = b"x-correlation-id"
TOOL_HEADER = b"x-assistant-tool"
_SAFE_CORRELATION = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_SAFE_TOOL = re.compile(r"^[a-z0-9_]{1,64}$")


def _header(scope: Scope, name: bytes, pattern: re.Pattern[str]) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            return text if pattern.match(text) else None
    return None


class InterventionRequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path") or "")
        if scope.get("type") != "http" or not (
            path == INTERVENTION_PATH_PREFIX or path.startswith(INTERVENTION_PATH_PREFIX + "/")
        ):
            await self.app(scope, receive, send)
            return
        status: dict[str, int] = {}
        observed: dict[str, str] = {}

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        started = time.monotonic()
        failed = False
        reset = AUTH_OBSERVER.set(observed)
        try:
            await self.app(scope, receive, send_wrapper)
        except BaseException:
            failed = True
            raise
        finally:
            AUTH_OBSERVER.reset(reset)
            self._log(scope, status.get("code"), failed, started, observed)

    @staticmethod
    def _log(
        scope: Scope, code: int | None, failed: bool, started: float, actor: dict[str, str]
    ) -> None:
        with log_guard():  # ログの故障で応答を変えない（INV-38）
            emit(
                logger,
                EventName.INTERVENTION_API_REQUEST,
                logging.INFO,
                "intervention api request",
                **_fields(scope, code, failed, started, actor),
            )


def _outcome(http_status: int | None) -> Outcome:
    if http_status is None or http_status >= 500:
        return Outcome.FAILED
    return Outcome.SUCCEEDED if http_status < 400 else Outcome.REJECTED


def _fields(
    scope: Scope, code: int | None, failed: bool, started: float, actor: dict[str, str]
) -> dict[str, Any]:
    http_status = 500 if failed and code is None else code
    episode_id = (scope.get("path_params") or {}).get("episode_id")
    return {
        "http_method": scope.get("method"),
        "http_route": getattr(scope.get("route"), "path", None),
        "http_status": http_status,
        "actor_type": actor.get("actor_type"),
        "actor_id": actor.get("actor_id"),
        "correlation_id": _header(scope, CORRELATION_HEADER, _SAFE_CORRELATION),
        "tool_name": _header(scope, TOOL_HEADER, _SAFE_TOOL),
        "episode_id": str(episode_id) if episode_id is not None else None,
        "outcome": _outcome(http_status).value,
        "duration_ms": (time.monotonic() - started) * 1000,
    }


__all__ = ["InterventionRequestLoggingMiddleware"]
