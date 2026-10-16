"""API のログ（pure ASGI middleware・resume のイベント・lifespan / log-contract §5・§8）。

理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from infrastructure.logging.asgi import RequestLoggingMiddleware
from tests.support.log_capture import capture_json
from tests.unit.test_resume_api import TO_STORYBOARD_READY, FakeResumeStarter, _episode


@pytest_asyncio.fixture
async def api(session_factory) -> AsyncIterator[tuple[AsyncClient, FakeResumeStarter]]:
    from apps.api.dependencies import get_session_factory, get_workflow_starter
    from apps.api.main import create_app

    starter = FakeResumeStarter()
    app = create_app()
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_workflow_starter] = lambda: starter
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, starter


async def test_health_is_logged_at_debug(api) -> None:
    client, _ = api
    with capture_json() as logs:
        assert (await client.get("/healthz")).status_code == 200
    [done] = logs.events("api.request.completed")
    assert done["level"] == "DEBUG"
    assert done["http_route"] == "/healthz" and done["http_status"] == 200
    assert isinstance(done["duration_ms"], float)


async def test_route_template_episode_id_and_request_id(api, session_factory) -> None:
    client, starter = api
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    with capture_json() as logs:
        response = await client.post(
            f"/episodes/{episode_id}/resume?x=secret", headers={"X-Request-ID": "req-123"}
        )
    assert response.status_code == 202
    [done] = logs.events("api.request.completed")
    assert done["level"] == "INFO"
    assert done["http_method"] == "POST"
    assert done["http_route"] == "/episodes/{episode_id}/resume"
    assert done["episode_id"] == str(episode_id)
    assert done["request_id"] == "req-123"
    assert "secret" not in logs.stream.getvalue()
    names = logs.names()
    assert names.index("episode.resume.requested") < names.index("episode.resume.started")
    [started] = logs.events("episode.resume.started")
    assert started["episode_id"] == str(episode_id)
    assert started["request_id"] == "req-123"
    assert started["workflow_id"] == f"episode-{episode_id}-pipeline"
    assert started["stage"] == "resume"


async def test_rejected_resume_is_logged_with_the_status(api) -> None:
    client, _ = api
    missing = uuid.uuid4()
    with capture_json() as logs:
        assert (await client.post(f"/episodes/{missing}/resume")).status_code == 404
    [rejected] = logs.events("episode.resume.rejected")
    assert rejected["http_status"] == 404
    assert rejected["episode_id"] == str(missing)


async def test_already_running_resume_is_rejected_with_409(api, session_factory) -> None:
    client, starter = api
    starter.already_running = True
    episode_id = await _episode(session_factory, TO_STORYBOARD_READY)
    with capture_json() as logs:
        assert (await client.post(f"/episodes/{episode_id}/resume")).status_code == 409
    [rejected] = logs.events("episode.resume.rejected")
    assert rejected["http_status"] == 409


@pytest.mark.parametrize("header", ["has space", "x" * 65, "a/b"])
async def test_unsafe_request_ids_are_replaced(api, header: str) -> None:
    client, _ = api
    with capture_json() as logs:
        await client.get("/healthz", headers={"X-Request-ID": header})
    [done] = logs.events("api.request.completed")
    assert done["request_id"] != header
    uuid.UUID(hex=done["request_id"])


async def test_an_exception_is_logged_and_reraised_unchanged() -> None:
    err = RuntimeError("boom")

    async def app(scope, receive, send):
        raise err

    middleware = RequestLoggingMiddleware(app)
    scope = {"type": "http", "method": "GET", "path": "/x", "headers": []}
    with capture_json() as logs, pytest.raises(RuntimeError) as caught:
        await middleware(scope, None, None)  # type: ignore[arg-type]
    assert caught.value is err
    [done] = logs.events("api.request.completed")
    assert done["http_status"] == 500 and done["outcome"] == "failed"


async def test_lifespan_logs_service_started_and_stopped() -> None:
    from apps.api.main import create_app

    app = create_app()
    with capture_json() as logs:
        async with app.router.lifespan_context(app):
            pass
    assert logs.names()[:2] == ["service.started", "service.stopped"]
