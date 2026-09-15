"""production 設定の既定値と契約定数の整合（ADR-0017 §4）。"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from contracts.production_activities import (
    AWAIT_HEARTBEAT_TIMEOUT_SECONDS,
    AWAIT_START_TO_CLOSE_SECONDS,
)
from infrastructure.config import Settings


@pytest.fixture(autouse=True)
def _no_ambient_fal_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_env_file=None`` は OS の環境変数を無視しない。実行環境の ``FAL_KEY`` に左右させない。"""
    # 例: ``FAL_KEY=`` が export された shell では ``fal_key`` が None でなく空の SecretStr になる
    monkeypatch.delenv("FAL_KEY", raising=False)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def test_await_deadline_is_shorter_than_the_activity_start_to_close() -> None:
    """期限切れを ProviderPollDeadlineError として返せるよう、Temporal に殺される前に止まる。"""
    assert _settings().production_await_timeout_seconds < AWAIT_START_TO_CLOSE_SECONDS


def test_fal_read_timeout_is_well_below_the_heartbeat_timeout() -> None:
    """1回の HTTP 待ちで heartbeat が途切れない。"""
    assert _settings().production_fal_read_timeout_seconds * 2 < AWAIT_HEARTBEAT_TIMEOUT_SECONDS


def test_fal_key_is_a_secret_that_does_not_leak() -> None:
    settings = _settings(fal_key="sk-very-secret-value")
    assert isinstance(settings.fal_key, SecretStr)
    assert settings.fal_key.get_secret_value() == "sk-very-secret-value"
    for rendered in (repr(settings), str(settings.fal_key), repr(settings.fal_key)):
        assert "sk-very-secret-value" not in rendered
    assert "sk-very-secret-value" not in str(settings.model_dump())
    assert _settings().fal_key is None
