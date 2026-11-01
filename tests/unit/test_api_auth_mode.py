"""API 認証の明示的な無効化 ``AVP_API_AUTH_MODE=disabled``（ADR-0080 追補 2026-11-01）。

既定は required。disabled は所有者が明示したときだけで、黙って無認証に倒れることはない。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from apps.api.auth import (
    Actor,
    ApiActor,
    ApiTokens,
    get_api_token_source,
    get_upload_approver,
    require_api_actor,
)
from apps.api.dependencies import get_settings
from contracts.approval import API_AUTH_MODE_ENV, ApiAuthMode, parse_api_auth_mode
from infrastructure.config import Settings

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 40


def _settings(mode: object | None) -> Settings:
    kw: dict[str, Any] = {} if mode is None else {API_AUTH_MODE_ENV: mode}
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


def _app(mode: object | None, tokens: ApiTokens) -> tuple[FastAPI, dict[str, int]]:
    reads = {"n": 0}

    def source() -> ApiTokens:
        reads["n"] += 1
        return tokens

    app = FastAPI()

    @app.post("/m", dependencies=[Depends(require_api_actor)])
    async def mutate(actor: ApiActor) -> dict[str, str | None]:
        return {
            "actor": actor.value if actor else None,
            "approver": str(get_upload_approver(actor)),
        }

    app.dependency_overrides[get_settings] = lambda: _settings(mode)
    app.dependency_overrides[get_api_token_source] = lambda: source
    return app, reads


async def _post(app: FastAPI, **headers: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        return await client.post("/m", headers=headers)


# ---- 設定の読み取り（fail closed）


def test_the_default_is_required() -> None:
    assert _settings(None).api_auth_mode is ApiAuthMode.REQUIRED


def test_only_an_explicit_disabled_turns_authentication_off() -> None:
    assert _settings("disabled").api_auth_mode is ApiAuthMode.DISABLED
    assert _settings(" Disabled ").api_auth_mode is ApiAuthMode.DISABLED
    assert _settings("required").api_auth_mode is ApiAuthMode.REQUIRED


@pytest.mark.parametrize("raw", ["", "off", "disable", "false", "0", "none", "no", None, 0, True])
def test_any_other_value_stays_required_and_does_not_stop_the_boot(raw: object) -> None:
    assert parse_api_auth_mode(raw) is ApiAuthMode.REQUIRED
    assert _settings(raw).api_auth_mode is ApiAuthMode.REQUIRED


def test_the_setting_reads_the_documented_environment_key(monkeypatch: pytest.MonkeyPatch) -> None:
    assert API_AUTH_MODE_ENV == "AVP_API_AUTH_MODE"
    monkeypatch.setenv("AVP_API_AUTH_MODE", "disabled")
    assert Settings(_env_file=None).api_auth_mode is ApiAuthMode.DISABLED  # type: ignore[call-arg]


# ---- require_api_actor


async def test_disabled_lets_a_mutation_through_without_reading_any_token_file(caplog) -> None:
    app, reads = _app("disabled", ApiTokens())
    with caplog.at_level(logging.INFO):
        response = await _post(app)
    assert response.status_code == 200
    assert response.json() == {"actor": None, "approver": "None"}
    assert reads["n"] == 0
    lines = [r.getMessage() for r in caplog.records if "AVP_API_AUTH_MODE" in r.getMessage()]
    assert len(lines) == 1 and "disabled" in lines[0] and "POST" in lines[0]


async def test_disabled_ignores_a_bad_credential_and_never_logs_it(caplog) -> None:
    app, _ = _app("disabled", ApiTokens())
    with caplog.at_level(logging.DEBUG):
        response = await _post(app, authorization=f"Bearer {TOKEN}")
    assert response.status_code == 200
    assert TOKEN not in caplog.text


async def test_required_with_no_tokens_is_still_503() -> None:
    for mode in (None, "required", "garbage", ""):
        app, reads = _app(mode, ApiTokens())
        assert (await _post(app)).status_code == 503, mode
        assert reads["n"] == 1


async def test_required_with_tokens_still_demands_a_valid_credential() -> None:
    app, _ = _app("required", ApiTokens.from_pairs([(Actor.OWNER, TOKEN)]))
    assert (await _post(app)).status_code == 401
    ok = await _post(app, authorization=f"Bearer {TOKEN}")
    assert ok.status_code == 200 and ok.json()["actor"] == "owner"


async def test_reads_never_log_the_disabled_notice(caplog) -> None:
    app = FastAPI()

    @app.get("/r", dependencies=[Depends(require_api_actor)])
    async def read() -> dict[str, str]:
        return {}

    app.dependency_overrides[get_settings] = lambda: _settings("disabled")
    with caplog.at_level(logging.INFO):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            assert (await client.get("/r")).status_code == 200
    assert "AVP_API_AUTH_MODE" not in caplog.text


def test_an_unauthenticated_request_never_becomes_an_approver() -> None:
    assert get_upload_approver(None) is None


# ---- compose


def test_compose_passes_the_mode_to_the_api_with_a_required_default() -> None:
    env = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))["services"]["api"][
        "environment"
    ]
    assert env[API_AUTH_MODE_ENV] == "${AVP_API_AUTH_MODE:-required}"
    assert Settings.model_fields["api_auth_mode"].validation_alias == API_AUTH_MODE_ENV
