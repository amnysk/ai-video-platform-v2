"""CORS は ``API_CORS_ORIGINS`` が空でない時だけ（ADR-0044 §4）。

理由は docs/testing/jpmas2-p2a-http-rationale.md。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.dependencies import get_settings
from apps.api.main import create_app


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _preflight(app: FastAPI, origin: str) -> httpx.Response:
    async def go() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://t",  # type: ignore[arg-type]
        ) as client:
            return await client.options(
                "/v1/intervention/episodes",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "authorization,x-correlation-id",
                },
            )

    import asyncio

    return asyncio.run(go())


def test_by_default_there_is_no_cors_middleware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("API_CORS_ORIGINS", raising=False)
    app = create_app()
    assert CORSMiddleware not in [m.cls for m in app.user_middleware]
    assert "access-control-allow-origin" not in _preflight(app, "http://ui.example").headers


def test_blank_origins_also_add_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", " , ")
    app = create_app()
    assert CORSMiddleware not in [m.cls for m in app.user_middleware]


def test_listed_origins_get_exactly_get_post_and_the_three_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", "http://ui.example, http://localhost:5173")
    app = create_app()
    allowed = _preflight(app, "http://localhost:5173")
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert set(allowed.headers["access-control-allow-methods"].split(", ")) == {"GET", "POST"}
    # Starlette は CORS 安全リストの header（accept 等）を常に足す。3 つが入り、全許可でない
    headers = {
        h.strip().lower() for h in allowed.headers["access-control-allow-headers"].split(",")
    }
    assert {"authorization", "content-type", "x-correlation-id"} <= headers
    assert "*" not in headers
    assert "access-control-allow-credentials" not in allowed.headers  # token は header で渡す

    other = _preflight(app, "http://evil.example")
    assert "access-control-allow-origin" not in other.headers
    assert other.status_code == 400


def test_a_wildcard_origin_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_CORS_ORIGINS", "*")
    with pytest.raises(ValueError, match="wildcard"):
        create_app()
