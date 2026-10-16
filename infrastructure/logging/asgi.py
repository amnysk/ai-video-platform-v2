"""API の1リクエストのログ（pure ASGI middleware / log-contract §5・§8）。

- ``request_id``: 受信 ``X-Request-ID`` が ``[A-Za-z0-9._-]{1,64}`` ならそれ、無ければ生成して
  束縛する
- ``api.request.completed``: ルーティング後の ``scope["route"]``（route template）と
  ``path_params["episode_id"]`` を読む（実 path・query は出さない）。health は DEBUG
- 例外は記録してから同じオブジェクトをそのまま再送出する（応答は変えない）

``BaseHTTPMiddleware`` は使わない（handler で束縛した文脈が戻らず、応答の流し方も変わる）。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from contracts.log_contract import EventName, Outcome
from infrastructure.logging.context import log_context
from infrastructure.logging.emit import emit

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("avp.api")

REQUEST_ID_HEADER = b"x-request-id"
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
#: DEBUG で出す route（死活監視が数秒ごとに叩く）
QUIET_ROUTES = frozenset({"/healthz"})


def _request_id(scope: Scope) -> str:
    for name, value in scope.get("headers") or ():
        if name.lower() == REQUEST_ID_HEADER:
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError:
                break
            if _SAFE_REQUEST_ID.match(text):
                return text
            break
    return uuid.uuid4().hex


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = _request_id(scope)
        status: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = int(message.get("status", 0))
            await send(message)

        started = time.monotonic()
        failed = False
        with log_context(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            except BaseException:
                failed = True
                raise
            finally:
                self._completed(scope, status.get("code"), failed, started)

    @staticmethod
    def _completed(scope: Scope, code: int | None, failed: bool, started: float) -> None:
        try:
            route = getattr(scope.get("route"), "path", None)
            params = scope.get("path_params") or {}
            episode_id = params.get("episode_id")
            http_status = 500 if failed and code is None else code
            ok = not failed and http_status is not None and http_status < 500
            emit(
                logger,
                EventName.API_REQUEST_COMPLETED,
                logging.DEBUG if route in QUIET_ROUTES else logging.INFO,
                "%s %s -> %s",
                scope.get("method"),
                route or "<unmatched>",
                http_status,
                http_method=scope.get("method"),
                http_route=route,
                episode_id=str(episode_id) if episode_id is not None else None,
                http_status=http_status,
                outcome=(Outcome.SUCCEEDED if ok else Outcome.FAILED).value,
                duration_ms=(time.monotonic() - started) * 1000,
            )
        except Exception:  # ログの故障で応答を変えない（INV-38）
            pass


__all__ = ["REQUEST_ID_HEADER", "RequestLoggingMiddleware"]
