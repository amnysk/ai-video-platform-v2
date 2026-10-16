"""infrastructure.temporal.connect.connect_with_retry の単体検査。

API（設計書どおり）:
    async def connect_with_retry(settings, *, connect=Client.connect, sleep=asyncio.sleep,
        initial_delay=1.0, max_delay=30.0,
        max_attempts=None) -> Client
connect は ``connect(settings.temporal_address, namespace=settings.temporal_namespace)``
で呼ばれる想定。
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from infrastructure.config import Settings
from infrastructure.temporal.connect import connect_with_retry

SECRET_MSG = "dial failed http://user:secret-token-xyz@temporal:7233"


class FakeConnect:
    def __init__(self, failures: int, exc: BaseException | None = None) -> None:
        self.failures = failures
        self.exc = exc or ConnectionError(SECRET_MSG)
        self.calls: list[tuple[tuple, dict]] = []
        self.client = object()

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if len(self.calls) <= self.failures:
            raise self.exc
        return self.client


class FakeSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _settings() -> Settings:
    return Settings(temporal_address="temporal:7233", temporal_namespace="default")


async def test_success_first_try() -> None:
    connect, sleep = FakeConnect(0), FakeSleep()
    client = await connect_with_retry(_settings(), connect=connect, sleep=sleep)
    assert client is connect.client
    assert len(connect.calls) == 1
    assert connect.calls[0][0][0] == "temporal:7233"
    assert sleep.delays == []


async def test_retries_with_capped_backoff() -> None:
    connect, sleep = FakeConnect(5), FakeSleep()
    client = await connect_with_retry(
        _settings(), connect=connect, sleep=sleep, initial_delay=1.0, max_delay=4.0
    )
    assert client is connect.client
    assert len(connect.calls) == 6
    assert sleep.delays == [1.0, 2.0, 4.0, 4.0, 4.0]


async def test_max_attempts_exhaustion_reraises_last_error() -> None:
    connect, sleep = FakeConnect(100, RuntimeError("boom")), FakeSleep()
    with pytest.raises(RuntimeError, match="boom"):
        await connect_with_retry(_settings(), connect=connect, sleep=sleep, max_attempts=3)
    assert len(connect.calls) == 3
    assert len(sleep.delays) == 2


async def test_logs_type_name_but_not_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    connect, sleep = FakeConnect(1), FakeSleep()
    await connect_with_retry(_settings(), connect=connect, sleep=sleep)
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings
    text = "\n".join(r.getMessage() for r in warnings)
    assert "ConnectionError" in text
    assert "secret-token-xyz" not in caplog.text


async def test_cancellation_propagates_without_retry() -> None:
    connect, sleep = FakeConnect(100, asyncio.CancelledError()), FakeSleep()
    with pytest.raises(asyncio.CancelledError):
        await connect_with_retry(_settings(), connect=connect, sleep=sleep)
    assert len(connect.calls) == 1
    assert sleep.delays == []
