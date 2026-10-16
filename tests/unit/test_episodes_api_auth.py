"""``EPISODES_API_AUTH_REQUIRED``（ADR-0045 §4）。理由は docs/testing/jpmas2-p2b-rationale.md。

実 FastAPI を組み、``/episodes/*`` と ``/research/*`` の全 route を route 表から列挙して試す
（route を足して依存を付け忘れると落ちる）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from apps.api.auth import parse_principals, token_sha256
from contracts.intervention import Scope
from infrastructure.config import Settings

READ_TOKEN = "episodes-read-token-0001"
RESUME_TOKEN = "episodes-resume-token-0001"
AI_TOKEN = "episodes-ai-token-0001"
AGENT_TOKEN = "episodes-agent-token-0001"
PRINCIPALS = json.dumps(
    {
        token_sha256(AI_TOKEN): {
            "actor_type": "ai_assistant",
            "actor_id": "assistant",
            "scopes": [Scope.READ.value, Scope.RESUME.value],
        },
        token_sha256(AGENT_TOKEN): {
            "actor_type": "agent",
            "actor_id": "agent",
            "scopes": [Scope.READ.value, Scope.RESUME.value],
        },
        token_sha256(READ_TOKEN): {
            "actor_type": "human",
            "actor_id": "reader",
            "scopes": [Scope.READ.value],
        },
        token_sha256(RESUME_TOKEN): {
            "actor_type": "human",
            "actor_id": "operator",
            "scopes": [Scope.READ.value, Scope.RESUME.value],
        },
    }
)


class _Starter:
    async def start_episode_workflow(self, **_: Any) -> str:
        return "wf"

    async def start_research(self, **_: Any) -> str:
        return "wf"


def _build(session_factory, artifact_store, *, required: bool):
    from apps.api.dependencies import get_session_factory, get_settings, get_workflow_starter
    from apps.api.episodes_auth import get_episodes_registry
    from apps.api.main import create_app
    from apps.api.routers.research import get_research_starter, get_research_store

    app = create_app()
    settings = Settings(_env_file=None, episodes_api_auth_required=required)  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_episodes_registry] = lambda: parse_principals(PRINCIPALS)
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: _Starter()
    app.dependency_overrides[get_research_starter] = lambda: _Starter()
    app.dependency_overrides[get_research_store] = lambda: artifact_store
    return app


def _routes(_app) -> list[tuple[str, str]]:
    from apps.api.routers import episodes, research

    found = [
        (method, route.path)
        for router in (episodes.router, research.router)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in sorted((route.methods or set()) - {"HEAD", "OPTIONS"})
    ]
    assert len(found) >= 10  # 列挙が空振りしていない
    return found


def _concrete(path: str) -> str:
    return path.replace("{episode_id}", str(uuid.uuid4())).replace(
        "{request_id}", str(uuid.uuid4())
    )


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def required(session_factory, artifact_store):
    app = _build(session_factory, artifact_store, required=True)
    async with _client(app) as client:
        yield app, client


@pytest_asyncio.fixture
async def optional(session_factory, artifact_store):
    app = _build(session_factory, artifact_store, required=False)
    async with _client(app) as client:
        yield app, client


async def test_required_every_route_without_a_token_is_401(required) -> None:
    app, client = required
    for method, path in _routes(app):
        r = await client.request(method, _concrete(path), json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.text)
        assert r.json()["detail"]["error"] == "unauthenticated"


async def test_required_a_wrong_token_is_401(required) -> None:
    app, client = required
    for method, path in _routes(app):
        r = await client.request(method, _concrete(path), headers=_bearer("nope-nope-nope"))
        assert r.status_code == 401, (method, path)


async def test_required_a_read_only_token_cannot_post_but_passes_get(required) -> None:
    app, client = required
    for method, path in _routes(app):
        r = await client.request(
            method,
            _concrete(path),
            headers=_bearer(READ_TOKEN),
            json={} if method == "POST" else None,
        )
        if method == "POST":
            assert r.status_code == 403, (method, path, r.text)
            assert r.json()["detail"] == {
                "error": "missing_scope",
                "required_scope": "resume",
                "message": "this principal lacks the 'resume' scope",
            }
        else:
            assert r.status_code not in (401, 403, 503), (method, path, r.text)


async def test_required_a_resume_token_passes_authentication_on_every_route(required) -> None:
    app, client = required
    for method, path in _routes(app):
        r = await client.request(
            method,
            _concrete(path),
            headers=_bearer(RESUME_TOKEN),
            json={} if method == "POST" else None,
        )
        assert r.status_code not in (401, 403, 503), (method, path, r.text)


async def test_required_a_valid_token_really_reaches_the_handler(required) -> None:
    _, client = required
    created = await client.post("/episodes", json={"topic": "x"}, headers=_bearer(RESUME_TOKEN))
    assert created.status_code == 202, created.text
    got = await client.get(f"/episodes/{created.json()['id']}", headers=_bearer(READ_TOKEN))
    assert got.status_code == 200


async def test_required_healthz_stays_open(required) -> None:
    _, client = required
    assert (await client.get("/healthz")).status_code == 200


async def test_required_without_configured_principals_fails_closed_503(
    session_factory, artifact_store
) -> None:
    from apps.api.episodes_auth import get_episodes_registry

    app = _build(session_factory, artifact_store, required=True)
    app.dependency_overrides[get_episodes_registry] = lambda: None
    async with _client(app) as client:
        r = await client.get(f"/episodes/{uuid.uuid4()}")
    assert r.status_code == 503


async def test_default_is_off_and_needs_no_token(optional) -> None:
    assert Settings(_env_file=None).episodes_api_auth_required is False  # type: ignore[call-arg]
    _, client = optional
    created = await client.post("/episodes", json={"topic": "x"})
    assert created.status_code == 202
    got = await client.get(f"/episodes/{created.json()['id']}")
    assert got.status_code == 200
    missing = await client.get(f"/episodes/{uuid.uuid4()}")
    assert missing.status_code == 404  # 401 ではない


async def test_off_ignores_even_an_invalid_token(optional) -> None:
    _, client = optional
    r = await client.get(f"/episodes/{uuid.uuid4()}", headers=_bearer("garbage-garbage"))
    assert r.status_code == 404


async def test_required_reads_the_same_intervention_principals_setting(
    session_factory, artifact_store
) -> None:
    from apps.api.dependencies import get_settings
    from apps.api.episodes_auth import get_episodes_registry

    app = _build(session_factory, artifact_store, required=True)
    del app.dependency_overrides[get_episodes_registry]
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,  # pyright: ignore[reportCallIssue]
        episodes_api_auth_required=True,
        intervention_principals=PRINCIPALS,  # pyright: ignore[reportArgumentType]
    )
    app.dependency_overrides[get_settings] = lambda: settings
    async with _client(app) as client:
        ok = await client.get(f"/episodes/{uuid.uuid4()}", headers=_bearer(READ_TOKEN))
        no = await client.get(f"/episodes/{uuid.uuid4()}")
    assert ok.status_code == 404 and no.status_code == 401


async def test_required_ai_principals_with_resume_cannot_post_but_can_read(required) -> None:
    """P1-1: scope だけでは足りない。有料生成・投稿に繋がる POST は人限定（ADR-0045 §4）。"""
    app, client = required
    for token in (AI_TOKEN, AGENT_TOKEN):
        for method, path in _routes(app):
            r = await client.request(
                method,
                _concrete(path),
                headers=_bearer(token),
                json={} if method == "POST" else None,
            )
            if method == "POST":
                assert r.status_code == 403, (method, path, r.text)
                detail = r.json()["detail"]
                assert detail["error"] == "forbidden"
                assert detail["reason"] == "human_only"
            else:
                assert r.status_code not in (401, 403, 503), (method, path, r.text)


async def test_required_ai_principal_cannot_reach_paid_routes_even_with_valid_bodies(
    required,
) -> None:
    _, client = required
    r = await client.post("/episodes", json={"topic": "x"}, headers=_bearer(AI_TOKEN))
    assert r.status_code == 403 and r.json()["detail"]["reason"] == "human_only"
    for suffix in ("production", "render", "upload", "resume"):
        r = await client.post(
            f"/episodes/{uuid.uuid4()}/{suffix}", json={}, headers=_bearer(AGENT_TOKEN)
        )
        assert r.status_code == 403, (suffix, r.text)


async def test_required_human_check_comes_after_scope_check(required) -> None:
    """read のみの AI は missing_scope（従来の理由を保つ）。"""
    _, client = required
    # READ_TOKEN は human。AI で resume を持たない主体は PRINCIPALS に無いので human で確認
    r = await client.post("/episodes", json={}, headers=_bearer(READ_TOKEN))
    assert r.json()["detail"]["error"] == "missing_scope"


async def test_every_episodes_and_research_route_of_the_full_app_is_guarded(required) -> None:
    """P1-1: 2 router ではなく create_app() 全体の route 表（OpenAPI）から列挙する。

    FastAPI の内部構造（_IncludedRouter 等）に依存せず、公開された route 表と振る舞いで確かめる:
    token なし → 401、AI の POST → 403 human_only。依存の付け忘れ・別 router の追加で落ちる。
    """
    app, client = required
    paths = app.openapi()["paths"]
    targets = [
        (method.upper(), path)
        for path, ops in paths.items()
        if path.startswith(("/episodes", "/research"))
        for method in ops
    ]
    assert len(targets) >= 10
    for method, path in targets:
        body = {} if method == "POST" else None
        anon = await client.request(method, _concrete(path), json=body)
        assert anon.status_code == 401, (method, path, anon.text)
        if method == "POST":
            ai = await client.request(method, _concrete(path), json=body, headers=_bearer(AI_TOKEN))
            assert ai.status_code == 403, (method, path, ai.text)
            assert ai.json()["detail"]["reason"] == "human_only"


async def test_docs_openapi_and_healthz_stay_unauthenticated_when_required(required) -> None:
    """ADR-0045 に既知・受容として記載。変えるなら ADR を先に。"""
    _, client = required
    for path in ("/healthz", "/openapi.json", "/docs", "/redoc"):
        assert (await client.get(path)).status_code == 200, path
