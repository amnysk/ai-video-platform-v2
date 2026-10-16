"""``/ui/`` の静的配信（ADR-0045 §1）。理由は docs/testing/jpmas2-p2b-rationale.md。

静的ディレクトリは差し替えられる（実 UI の資産には依存しない）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from infrastructure.config import Settings

EXPECTED_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "cache-control": "no-store",
}


@pytest.fixture
def static_root(tmp_path: Path) -> Path:
    root = tmp_path / "static"
    (root / "sub").mkdir(parents=True)
    (root / "index.html").write_text("<h1>home</h1>", encoding="utf-8")
    (root / "app.js").write_text("export const a = 1;", encoding="utf-8")
    (root / "sub" / "index.html").write_text("<h1>sub</h1>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    return root


def _app(root: Path, *, flag: bool):
    from apps.api.dependencies import get_settings
    from apps.api.main import create_app
    from apps.api.routers.ui import get_ui_static_dir

    app = create_app()
    settings = Settings(_env_file=None, jpmas2_application_api=flag)  # type: ignore[call-arg]
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_ui_static_dir] = lambda: root
    return app


@pytest_asyncio.fixture
async def on(static_root: Path):
    async with AsyncClient(
        transport=ASGITransport(app=_app(static_root, flag=True)), base_url="http://test"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def off(static_root: Path):
    async with AsyncClient(
        transport=ASGITransport(app=_app(static_root, flag=False)), base_url="http://test"
    ) as client:
        yield client


def _assert_secure(response) -> None:
    for name, value in EXPECTED_HEADERS.items():
        assert response.headers.get(name) == value, name


async def test_on_serves_index_files_and_subdirectory_index_with_security_headers(on) -> None:
    for path, body in (
        ("/ui/", "<h1>home</h1>"),
        ("/ui/index.html", "<h1>home</h1>"),
        ("/ui/app.js", "export const a = 1;"),
        ("/ui/sub/", "<h1>sub</h1>"),
    ):
        r = await on.get(path)
        assert r.status_code == 200, path
        assert r.text == body
        _assert_secure(r)
    assert (await on.get("/ui/")).headers["content-type"].startswith("text/html")


async def test_on_unknown_file_is_404_with_security_headers(on) -> None:
    r = await on.get("/ui/nope.js")
    assert r.status_code == 404
    _assert_secure(r)


async def test_off_every_ui_path_is_the_same_404_feature_disabled_as_the_api(off) -> None:
    api = await off.get("/v1/intervention/episodes")
    assert api.status_code == 404 and api.json()["detail"]["error"] == "feature_disabled"
    for path in ("/ui/", "/ui/index.html", "/ui/app.js", "/ui/nope"):
        r = await off.get(path)
        assert r.status_code == 404, path
        assert r.json()["detail"]["error"] == "feature_disabled", path
        assert "home" not in r.text


async def test_off_does_not_touch_the_files(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    async with AsyncClient(
        transport=ASGITransport(app=_app(missing, flag=False)), base_url="http://test"
    ) as client:
        r = await client.get("/ui/")
    assert r.json()["detail"]["error"] == "feature_disabled"


async def test_on_with_a_missing_static_directory_is_404_not_500(tmp_path: Path) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app(tmp_path / "does-not-exist", flag=True)),
        base_url="http://test",
    ) as client:
        assert (await client.get("/ui/")).status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/ui/../secret.txt",
        "/ui/%2e%2e/secret.txt",
        "/ui/%2E%2E%2Fsecret.txt",
        "/ui/..%2fsecret.txt",
        "/ui/sub/../../secret.txt",
        "/ui/sub/%2e%2e/%2e%2e/secret.txt",
        "/ui/..%5csecret.txt",
        "/ui//etc/passwd",
        "/ui/%2Fetc%2Fpasswd",
        "/ui/app.js%00.png",
        "/ui/../",
        "/ui/%2e%2e/",
    ],
)
async def test_path_traversal_never_leaves_the_static_directory(on, path: str) -> None:
    r = await on.get(path)
    assert r.status_code == 404, path
    assert "TOP-SECRET" not in r.text and "root:" not in r.text


async def test_a_symlink_pointing_outside_is_not_served(on, static_root: Path) -> None:
    (static_root / "link.txt").symlink_to(static_root.parent / "secret.txt")
    r = await on.get("/ui/link.txt")
    assert r.status_code == 404
    assert "TOP-SECRET" not in r.text


async def test_ui_is_get_only(on) -> None:
    assert (await on.post("/ui/")).status_code == 405


async def _raw_get(app, path: str) -> tuple[int, bytes]:
    """HTTP client は ``..`` を畳むので、畳まれない生の path を ASGI に直接渡す。"""
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(b"host", b"test")],
        "server": ("test", 80),
        "client": ("test", 1),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


@pytest.mark.parametrize(
    "path",
    [
        "/ui/../secret.txt",
        "/ui/sub/../../secret.txt",
        "/ui/..",
        "/ui/..\\secret.txt",
        "/ui/a/../../x",
    ],
)
async def test_unnormalised_dot_dot_paths_reach_the_handler_and_are_refused(
    static_root: Path, path: str
) -> None:
    status, body = await _raw_get(_app(static_root, flag=True), path)
    assert status == 404, path
    assert b"TOP-SECRET" not in body


async def test_unnormalised_dot_dot_is_feature_disabled_when_off(static_root: Path) -> None:
    status, body = await _raw_get(_app(static_root, flag=False), "/ui/../secret.txt")
    assert status == 404 and b"feature_disabled" in body


async def test_on_ui_without_trailing_slash_redirects_to_ui_slash(on) -> None:
    # 相対 URL（./app.css, ./js/main.js）は末尾スラッシュが無いと /app.css に解決されて壊れる
    r = await on.get("/ui", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/ui/")
    _assert_secure(r)
    followed = await on.get("/ui", follow_redirects=True)
    assert followed.status_code == 200
    assert followed.text == "<h1>home</h1>"


async def test_off_ui_without_trailing_slash_is_404_feature_disabled_not_a_redirect(off) -> None:
    r = await off.get("/ui", follow_redirects=False)
    assert r.status_code == 404
    assert r.json()["detail"]["error"] == "feature_disabled"
