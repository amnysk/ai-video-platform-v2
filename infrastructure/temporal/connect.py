"""Temporal への接続を指数バックオフで再試行する（compose 起動順の揺らぎに耐える）。

ログには例外の型名だけを出す（メッセージは URL 等を含みうるため出さない）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from temporalio.client import Client

from infrastructure.config import Settings

logger = logging.getLogger(__name__)


async def connect_with_retry(
    settings: Settings,
    *,
    connect: Callable[..., Awaitable[Any]] = Client.connect,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
    max_attempts: int | None = None,
) -> Client:
    attempt = 0
    delay = initial_delay
    while True:
        attempt += 1
        try:
            return await connect(settings.temporal_address, namespace=settings.temporal_namespace)
        except Exception as exc:
            if max_attempts is not None and attempt >= max_attempts:
                logger.error(
                    "temporal connect failed: attempt=%d address=%s error=%s (giving up)",
                    attempt,
                    settings.temporal_address,
                    type(exc).__name__,
                )
                raise
            logger.warning(
                "temporal connect failed: attempt=%d address=%s error=%s; retry in %.1fs",
                attempt,
                settings.temporal_address,
                type(exc).__name__,
                delay,
            )
        await sleep(delay)
        delay = min(delay * 2, max_delay)
