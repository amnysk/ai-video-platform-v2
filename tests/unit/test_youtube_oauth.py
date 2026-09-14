"""OAuth refresh（Phase 6）。実ネットワークに出ない（MockTransport）。"""

from __future__ import annotations

import asyncio
import pathlib
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from infrastructure.youtube.errors import YouTubeAuthError, YouTubeTransientError
from infrastructure.youtube.oauth import (
    TOKEN_ENDPOINT,
    RefreshTokenCredentials,
    load_refresh_token,
)

REFRESH = "1//refresh-secret-value"
CLIENT_SECRET = "client-secret-value"


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _creds(handler, clock=None) -> RefreshTokenCredentials:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RefreshTokenCredentials(
        "client-id",
        SecretStr(CLIENT_SECRET),
        SecretStr(REFRESH),
        client=client,
        clock=clock or Clock(),
    )


async def test_refresh_posts_form_and_caches_until_expiry() -> None:
    calls: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == TOKEN_ENDPOINT
        calls.append(parse_qs(request.content.decode()))
        return httpx.Response(200, json={"access_token": f"at-{len(calls)}", "expires_in": 3600})

    clock = Clock()
    creds = _creds(handler, clock)
    assert await creds.access_token() == "at-1"
    assert await creds.access_token() == "at-1"
    assert calls[0]["grant_type"] == ["refresh_token"]
    assert calls[0]["refresh_token"] == [REFRESH]
    clock.now += 3600 - 59
    assert await creds.access_token() == "at-2"
    creds.invalidate()
    assert await creds.access_token() == "at-3"


async def test_concurrent_callers_refresh_once() -> None:
    count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})

    creds = _creds(handler)
    assert await asyncio.gather(*(creds.access_token() for _ in range(5))) == ["at"] * 5
    assert count == 1


@pytest.mark.parametrize("error", ["invalid_grant", "invalid_client", "unauthorized_client"])
async def test_rejected_refresh_is_auth_error_without_secrets(error: str) -> None:
    creds = _creds(
        lambda r: httpx.Response(400, json={"error": error, "error_description": REFRESH})
    )
    with pytest.raises(YouTubeAuthError) as info:
        await creds.access_token()
    assert error in str(info.value)
    assert REFRESH not in str(info.value) and CLIENT_SECRET not in str(info.value)


async def test_server_error_and_transport_are_transient() -> None:
    with pytest.raises(YouTubeTransientError):
        await _creds(lambda r: httpx.Response(503)).access_token()

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect {REFRESH}")

    with pytest.raises(YouTubeTransientError) as info:
        await _creds(boom).access_token()
    assert REFRESH not in str(info.value) and info.value.__cause__ is None


def test_repr_is_redacted() -> None:
    creds = _creds(lambda r: httpx.Response(500))
    assert REFRESH not in repr(creds) and CLIENT_SECRET not in str(creds)


def test_load_refresh_token_checks_permissions_and_location(tmp_path: pathlib.Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "token"
    outside.write_text(REFRESH + "\n")
    outside.chmod(0o644)
    with pytest.raises(YouTubeAuthError, match="0600"):
        load_refresh_token(outside, repo_root=repo)
    outside.chmod(0o600)
    assert load_refresh_token(outside, repo_root=repo).get_secret_value() == REFRESH

    inside = repo / "token"
    inside.write_text(REFRESH)
    inside.chmod(0o600)
    with pytest.raises(YouTubeAuthError, match="outside the repository"):
        load_refresh_token(inside, repo_root=repo)
    with pytest.raises(YouTubeAuthError, match="not readable"):
        load_refresh_token(tmp_path / "missing", repo_root=repo)
    empty = tmp_path / "empty"
    empty.write_text("")
    empty.chmod(0o600)
    with pytest.raises(YouTubeAuthError, match="empty"):
        load_refresh_token(empty, repo_root=repo)
