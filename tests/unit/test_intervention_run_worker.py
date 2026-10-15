"""介入 worker の起動時の警告（換算レート未設定だと有料の変更が通らない）。"""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from infrastructure.config import Settings
from workers.intervention import run_worker


class _Stop(Exception):
    pass


async def _start(monkeypatch: pytest.MonkeyPatch, rate: Decimal | None) -> None:
    settings = Settings(budget_usd_jpy_rate=rate)
    monkeypatch.setattr(run_worker, "Settings", lambda: settings)

    async def connect(_settings: object) -> object:
        raise _Stop

    monkeypatch.setattr(run_worker, "connect_with_retry", connect)
    with pytest.raises(_Stop):
        await run_worker.main()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "BUDGET_USD_JPY_RATE" in r.getMessage()]


async def test_unset_rate_warns_once_at_startup(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING)
    await _start(monkeypatch, None)
    assert len(_warnings(caplog)) == 1


async def test_a_configured_rate_does_not_warn(monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING)
    await _start(monkeypatch, Decimal("150"))
    assert _warnings(caplog) == []
