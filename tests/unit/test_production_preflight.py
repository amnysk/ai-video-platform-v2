"""Production provider preflight（ADR-0030）: 検証済み/未検証を混同しないこと。"""

from __future__ import annotations

from pydantic import SecretStr

from infrastructure.config import Settings
from infrastructure.production.preflight import CONFIRMED_NONBILLING_ENV, check_fal_credentials


class _FakeStorageClient:
    """テスト用の ``FalStorageClient`` 差し替え。"""

    fail_with: Exception | None = None

    def __init__(self, api_key: str, *, timeout_seconds: float) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.closed = False

    async def _token(self) -> tuple[str, str]:
        if self.fail_with is not None:
            raise self.fail_with
        return "Bearer", "tok"

    async def aclose(self) -> None:
        self.closed = True


def _settings(fal_key: str | None) -> Settings:
    return Settings(fal_key=SecretStr(fal_key) if fal_key else None)


async def test_missing_fal_key_fails_without_calling_anything() -> None:
    checks = await check_fal_credentials(_settings(None), env={})
    assert len(checks) == 1
    assert checks[0].status == "FAIL"
    assert "FAL_KEY" in checks[0].detail


async def test_present_key_without_confirmation_is_unverified_not_healthy() -> None:
    checks = await check_fal_credentials(_settings("secret"), env={})
    statuses = [c.status for c in checks]
    assert statuses == ["OK", "UNVERIFIED"]
    assert not any(c.is_failure for c in checks)
    # 未検証は healthy 扱いにしない: 呼び出し側はこれを「全部 OK」と読み違えてはいけない
    assert any(c.status == "UNVERIFIED" for c in checks)


async def test_confirmed_nonbilling_makes_a_real_call_and_reports_success() -> None:
    checks = await check_fal_credentials(
        _settings("secret"),
        client_factory=_FakeStorageClient,
        env={CONFIRMED_NONBILLING_ENV: "1"},
    )
    assert [c.status for c in checks] == ["OK", "OK"]


async def test_confirmed_nonbilling_reports_a_real_failure() -> None:
    class _Failing(_FakeStorageClient):
        fail_with = RuntimeError("HTTP 403")

    checks = await check_fal_credentials(
        _settings("secret"),
        client_factory=_Failing,
        env={CONFIRMED_NONBILLING_ENV: "1"},
    )
    assert [c.status for c in checks] == ["OK", "FAIL"]
    assert "403" in checks[-1].detail


async def test_secret_value_never_appears_in_check_output() -> None:
    checks = await check_fal_credentials(_settings("super-secret-value"), env={})
    for check in checks:
        assert "super-secret-value" not in check.detail


async def test_every_check_declares_whether_it_left_the_local_process() -> None:
    """運用者が出力を見て「どれが実ネットワーク呼び出しか」を一目で区別できること。"""
    # FAL_KEY確認・UNVERIFIED判定とも外部通信なし
    local_only = await check_fal_credentials(_settings("secret"), env={})
    assert [c.scope for c in local_only] == ["local", "local"]

    with_call = await check_fal_credentials(
        _settings("secret"),
        client_factory=_FakeStorageClient,
        env={CONFIRMED_NONBILLING_ENV: "1"},
    )
    assert [c.scope for c in with_call] == ["local", "network"]
