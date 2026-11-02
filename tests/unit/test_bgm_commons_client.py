"""Commons クライアント。ネットワークには出ない（httpx.MockTransport）。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from infrastructure.music.sources.commons import CommonsClient, validate_user_agent
from infrastructure.music.sources.errors import CandidateInvalidError, SourceUnavailableError

UA = "avp2-bgm-test/0.1 (https://example.invalid/contact; ops@example.invalid)"
FIX = Path(__file__).resolve().parents[1] / "fixtures" / "bgm_commons"
FILE_URL = "https://upload.wikimedia.org/wikipedia/commons/4/49/x.flac"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """INV-152: 収集のテストは通信しない。ソケットの接続が起きたら即失敗させる。"""
    import socket

    def refuse(self: socket.socket, *args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted in an acquisition test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class Recorder:
    def __init__(self, handler):
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def _client(handler, **kw) -> tuple[CommonsClient, Recorder, list[float]]:
    rec = Recorder(handler)
    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    return (
        CommonsClient(user_agent=UA, transport=httpx.MockTransport(rec), sleep=sleep, **kw),
        rec,
        slept,
    )


@pytest.mark.parametrize(
    "ua",
    ["", "python-requests/2.0", "curl/8", "Mozilla/5.0 (X11)", "bot/1.0", "bot/1 (no contact)"],
)
def test_user_agent_must_identify_the_client(ua: str) -> None:
    with pytest.raises(ValueError):
        validate_user_agent(ua)
    assert validate_user_agent(UA) == UA


async def test_search_sends_policy_headers_and_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"query": {"search": [{"title": "File:A.flac"}, {"title": "File:B.ogg"}]}}
        )

    c, rec, _ = _client(handler)
    assert await c.search_titles('filetype:audio incategory:"CC-Zero" ambient') == [
        "File:A.flac",
        "File:B.ogg",
    ]
    req = rec.requests[0]
    assert req.headers["user-agent"] == UA
    q = dict(req.url.params)
    assert req.url.host == "commons.wikimedia.org" and req.url.path == "/w/api.php"
    assert q["list"] == "search" and q["srnamespace"] == "6" and q["maxlag"] == "5"
    assert q["format"] == "json"
    await c.aclose()


async def test_imageinfo_returns_pages_in_title_order_and_batches() -> None:
    page = json.loads((FIX / "ck61.json").read_text("utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        titles = request.url.params["titles"].split("|")
        assert request.url.params["prop"] == "imageinfo|categories|templates"
        assert (
            "extmetadata" in request.url.params["iiprop"] and "sha1" in request.url.params["iiprop"]
        )
        pages = [{"title": t, "imageinfo": [{"sha1": "0" * 40}]} for t in titles]
        return httpx.Response(200, json={"query": {"pages": list(reversed(pages))}})

    c, rec, slept = _client(handler)
    titles = [f"File:{i}.ogg" for i in range(60)]
    got = await c.imageinfo(titles)
    assert [p["title"] for p in got] == titles
    assert len(rec.requests) == 2  # 50 + 10
    assert slept == [1.0]  # 2 回目の前だけ間隔を空ける（直列）
    assert page  # fixture is loadable
    await c.aclose()


async def test_requests_are_serialised() -> None:
    import asyncio

    active = 0
    peak = 0

    async def handler_async(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json={"query": {"search": []}})

    async def sleep(_: float) -> None:
        return None

    c = CommonsClient(user_agent=UA, transport=httpx.MockTransport(handler_async), sleep=sleep)
    await asyncio.gather(*(c.search_titles("x") for _ in range(4)))
    assert peak == 1
    await c.aclose()


async def test_maxlag_is_retried_once_then_reported_unavailable() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, headers={"Retry-After": "7"}, json={"error": {"code": "maxlag"}})

    c, _, slept = _client(handler)
    with pytest.raises(SourceUnavailableError):
        await c.search_titles("x")
    assert calls["n"] == 2 and 7.0 in slept
    await c.aclose()


async def test_maxlag_then_success() -> None:
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"error": {"code": "maxlag"}})
        return httpx.Response(200, json={"query": {"search": [{"title": "File:A.ogg"}]}})

    c, _, _ = _client(handler)
    assert await c.search_titles("x") == ["File:A.ogg"]
    await c.aclose()


@pytest.mark.parametrize("status", [403, 429, 500, 503])
async def test_http_errors_are_transient(status: int) -> None:
    c, _, _ = _client(lambda r: httpx.Response(status))
    with pytest.raises(SourceUnavailableError):
        await c.search_titles("x")
    await c.aclose()


async def test_timeout_and_connect_errors_are_transient() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("t")

    c, _, _ = _client(boom)
    with pytest.raises(SourceUnavailableError):
        await c.search_titles("x")
    with pytest.raises(SourceUnavailableError):
        await c.download(FILE_URL, expected_sha1="0" * 40)
    await c.aclose()


async def test_invalid_json_and_api_error_are_unavailable() -> None:
    c, _, _ = _client(lambda r: httpx.Response(200, content=b"<html>"))
    with pytest.raises(SourceUnavailableError):
        await c.search_titles("x")
    c2, _, _ = _client(lambda r: httpx.Response(200, json={"error": {"code": "readonly"}}))
    with pytest.raises(SourceUnavailableError):
        await c2.search_titles("x")
    await c.aclose()
    await c2.aclose()


async def test_download_verifies_sha1_and_returns_sha256() -> None:
    body = b"abc" * 1000
    sha1 = hashlib.sha1(body).hexdigest()  # noqa: S324
    c, rec, _ = _client(lambda r: httpx.Response(200, content=body))
    got = await c.download(FILE_URL, expected_sha1=sha1)
    assert got.data == body and got.extension == "flac"
    assert got.sha256 == hashlib.sha256(body).hexdigest()
    assert rec.requests[0].headers["user-agent"] == UA
    with pytest.raises(CandidateInvalidError) as exc:
        await c.download(FILE_URL, expected_sha1="0" * 40)
    assert exc.value.reason == "sha1_mismatch"
    await c.aclose()


async def test_download_byte_cap_declared_and_streamed() -> None:
    c, _, _ = _client(
        lambda r: httpx.Response(200, headers={"Content-Length": "5000"}, content=b"x" * 5000),
        max_bytes_per_file=1000,
    )
    with pytest.raises(CandidateInvalidError) as exc:
        await c.download(FILE_URL, expected_sha1="0" * 40)
    assert exc.value.reason == "too_large"

    # Content-Length が嘘 / 無い場合も流しながら打ち切る
    async def gen():
        for _ in range(10):
            yield b"x" * 400

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            async for part in gen():
                yield part

    c2, _, _ = _client(lambda r: httpx.Response(200, stream=Stream()), max_bytes_per_file=1000)
    with pytest.raises(CandidateInvalidError) as exc2:
        await c2.download(FILE_URL, expected_sha1="0" * 40)
    assert exc2.value.reason == "too_large"
    await c.aclose()
    await c2.aclose()


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://upload.wikimedia.org/a/x.flac", "host_not_allowed"),
        ("https://evil.example/a/x.flac", "host_not_allowed"),
        ("https://upload.wikimedia.org.evil.example/x.flac", "host_not_allowed"),
        ("https://upload.wikimedia.org/a/x.exe", "extension_not_allowed"),
        ("https://upload.wikimedia.org/a/x", "extension_not_allowed"),
    ],
)
async def test_download_url_and_extension_allowlist_no_request_made(url: str, reason: str) -> None:
    c, rec, _ = _client(lambda r: httpx.Response(200, content=b"x"))
    with pytest.raises(CandidateInvalidError) as exc:
        await c.download(url, expected_sha1="0" * 40)
    assert exc.value.reason == reason and rec.requests == []
    await c.aclose()


async def test_download_gone_is_candidate_invalid_and_5xx_transient() -> None:
    c, _, _ = _client(lambda r: httpx.Response(404))
    with pytest.raises(CandidateInvalidError):
        await c.download(FILE_URL, expected_sha1="0" * 40)
    c2, _, _ = _client(lambda r: httpx.Response(503))
    with pytest.raises(SourceUnavailableError):
        await c2.download(FILE_URL, expected_sha1="0" * 40)
    await c.aclose()
    await c2.aclose()


async def test_redirects_are_not_followed() -> None:
    c, rec, _ = _client(
        lambda r: httpx.Response(302, headers={"Location": "https://evil.example/x.flac"})
    )
    with pytest.raises(CandidateInvalidError) as exc:
        await c.download(FILE_URL, expected_sha1="0" * 40)
    assert exc.value.reason == "redirect_not_followed" and len(rec.requests) == 1
    await c.aclose()
