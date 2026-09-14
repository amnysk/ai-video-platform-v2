"""fal CDN v3 アップロード（非課金の準備工程）の HTTP と失敗の写像（ADR-0017 Phase 4C）。"""

from __future__ import annotations

import json

import httpx
import pytest

from domain.errors import (
    ProviderInvocationError,
    ProviderRejectedError,
    ProviderUnavailableError,
)
from infrastructure.providers.fal_storage import (
    CDN_UPLOAD_URL,
    STORAGE_TOKEN_URL,
    FalStorageClient,
)

TOKEN_OK = httpx.Response(
    200,
    json={
        "token": "tok",
        "token_type": "Bearer",
        "base_url": "https://v3.fal.media",
        "expires_at": "2099-01-01T00:00:00+00:00",
    },
)


def _client(routes) -> tuple[FalStorageClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        route = routes[str(request.url)]
        if isinstance(route, Exception):
            raise route
        return route

    return FalStorageClient("secret-key", transport=httpx.MockTransport(handler)), seen


async def test_token_then_upload_returns_access_url() -> None:
    client, seen = _client(
        {
            STORAGE_TOKEN_URL: TOKEN_OK,
            CDN_UPLOAD_URL: httpx.Response(200, json={"access_url": "https://v3.fal.media/f/x"}),
        }
    )
    url = await client.upload(b"PNG", "image/png", "a.png")
    assert url == "https://v3.fal.media/f/x"

    token_req, upload_req = seen
    assert token_req.method == "POST" and token_req.headers["authorization"] == "Key secret-key"
    assert json.loads(token_req.content) == {}
    assert upload_req.method == "POST" and upload_req.content == b"PNG"
    assert upload_req.headers["authorization"] == "Bearer tok"
    assert upload_req.headers["content-type"] == "image/png"
    assert upload_req.headers["x-fal-file-name"] == "a.png"
    assert json.loads(upload_req.headers["x-fal-object-lifecycle"]) == {
        "expiration_duration_seconds": 86400
    }
    assert "secret-key" not in upload_req.headers["authorization"]


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_needs_input(status) -> None:
    client, _ = _client({STORAGE_TOKEN_URL: httpx.Response(status)})
    with pytest.raises(ProviderUnavailableError) as info:
        await client.upload(b"PNG", "image/png", "a.png")
    assert "secret-key" not in str(info.value)


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_upload_5xx_is_retryable(status) -> None:
    client, _ = _client({STORAGE_TOKEN_URL: TOKEN_OK, CDN_UPLOAD_URL: httpx.Response(status)})
    with pytest.raises(ProviderInvocationError):
        await client.upload(b"PNG", "image/png", "a.png")


async def test_network_failure_is_retryable() -> None:
    client, _ = _client({STORAGE_TOKEN_URL: httpx.ConnectError("boom")})
    with pytest.raises(ProviderInvocationError):
        await client.upload(b"PNG", "image/png", "a.png")


async def test_other_4xx_is_rejected_and_bad_body_is_retryable() -> None:
    client, _ = _client({STORAGE_TOKEN_URL: TOKEN_OK, CDN_UPLOAD_URL: httpx.Response(413)})
    with pytest.raises(ProviderRejectedError):
        await client.upload(b"PNG", "image/png", "a.png")
    client, _ = _client(
        {STORAGE_TOKEN_URL: TOKEN_OK, CDN_UPLOAD_URL: httpx.Response(200, json={"x": 1})}
    )
    with pytest.raises(ProviderInvocationError):
        await client.upload(b"PNG", "image/png", "a.png")
