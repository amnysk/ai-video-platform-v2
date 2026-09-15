"""YouTube resumable uploader（Phase 6）。実ネットワークに出ない（MockTransport）。"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from pydantic import SecretStr

from domain.upload.ports import (
    UploadCompleted,
    UploadExpired,
    UploadIncomplete,
    UploadSessionRef,
    VideoUploader,
)
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeRejectedError,
    YouTubeTransientError,
)
from infrastructure.youtube.oauth import TOKEN_ENDPOINT, RefreshTokenCredentials
from infrastructure.youtube.uploader import (
    API_BASE_URL,
    CHUNK_UNIT_BYTES,
    UPLOAD_URL,
    YouTubeResumableUploader,
    redact,
    validate_chunk_bytes,
)

UPLOAD_ID = "AEnB2Uo-secret-upload-id"
SESSION_URI = f"{UPLOAD_URL}?uploadType=resumable&upload_id={UPLOAD_ID}"
METADATA = {
    "snippet": {"title": "t", "tags": ["avp-marker"]},
    "status": {"privacyStatus": "private"},
}
Handler = Callable[[httpx.Request], httpx.Response]


class Api:
    """token endpoint を肩代わりし、それ以外を ``handler`` に渡す。"""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.token_calls = 0
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            self.token_calls += 1
            return httpx.Response(
                200, json={"access_token": f"at-{self.token_calls}", "expires_in": 3600}
            )
        self.requests.append(request)
        return self.handler(request)


def _uploader(handler: Handler, **kwargs) -> tuple[YouTubeResumableUploader, Api]:
    api = Api(handler)
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    creds = RefreshTokenCredentials("cid", SecretStr("cs"), SecretStr("rt"), client=client)
    return YouTubeResumableUploader(creds, client=client, **kwargs), api


def _session(total: int = 1000) -> UploadSessionRef:
    return UploadSessionRef(uri=SESSION_URI, total_bytes=total)


def _error(status: int, reason: str) -> httpx.Response:
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": SESSION_URI, "errors": [{"reason": reason}]}},
    )


def test_satisfies_the_port() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(500))
    port: VideoUploader = uploader
    assert port is uploader


async def test_start_session_sends_resumable_initiation() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(200, headers={"Location": SESSION_URI}))
    ref = await uploader.start_session(METADATA, 12345, "video/mp4")
    assert ref == UploadSessionRef(uri=SESSION_URI, total_bytes=12345)
    (req,) = api.requests
    assert req.method == "POST"
    assert str(req.url).startswith(UPLOAD_URL)
    assert req.url.params["uploadType"] == "resumable"
    assert req.url.params["part"] == "snippet,status"
    assert req.url.params["notifySubscribers"] == "false"
    assert req.headers["X-Upload-Content-Length"] == "12345"
    assert req.headers["X-Upload-Content-Type"] == "video/mp4"
    assert req.headers["Authorization"] == "Bearer at-1"
    assert json.loads(req.content) == METADATA
    assert UPLOAD_ID not in repr(ref)


async def test_start_session_refuses_non_private_metadata() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(200, headers={"Location": SESSION_URI}))
    for status in ({"privacyStatus": "public"}, {}, None):
        meta = {"snippet": {}, "status": status} if status is not None else {"snippet": {}}
        with pytest.raises(YouTubeRejectedError):
            await uploader.start_session(meta, 10, "video/mp4")
    assert api.requests == []


async def test_start_session_without_or_with_foreign_location_is_transient() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(200))
    with pytest.raises(YouTubeTransientError):
        await uploader.start_session(METADATA, 10, "video/mp4")
    uploader, _ = _uploader(
        lambda r: httpx.Response(200, headers={"Location": "https://evil.example/upload"})
    )
    with pytest.raises(YouTubeTransientError):
        await uploader.start_session(METADATA, 10, "video/mp4")


async def test_send_chunk_sets_content_range_and_parses_308() -> None:
    chunk = b"x" * CHUNK_UNIT_BYTES
    total = CHUNK_UNIT_BYTES * 3
    uploader, api = _uploader(
        lambda r: httpx.Response(308, headers={"Range": f"bytes=0-{2 * CHUNK_UNIT_BYTES - 1}"}),
        chunk_bytes=CHUNK_UNIT_BYTES,
    )
    progress = await uploader.send_chunk(_session(total), CHUNK_UNIT_BYTES, chunk, total)
    assert progress == UploadIncomplete(next_offset=2 * CHUNK_UNIT_BYTES)
    (req,) = api.requests
    assert req.method == "PUT" and str(req.url) == SESSION_URI
    assert (
        req.headers["Content-Range"]
        == f"bytes {CHUNK_UNIT_BYTES}-{2 * CHUNK_UNIT_BYTES - 1}/{total}"
    )
    assert req.content == chunk
    assert req.headers["Content-Type"] == "video/mp4"


async def test_308_without_range_means_zero() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(308))
    assert await uploader.query_status(_session()) == UploadIncomplete(0)


@pytest.mark.parametrize("header", ["bytes=5-9", "garbage", "bytes=0-5000"])
async def test_unreadable_range_is_transient(header: str) -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(308, headers={"Range": header}))
    with pytest.raises(YouTubeTransientError):
        await uploader.query_status(_session(1000))


@pytest.mark.parametrize("status", [200, 201])
async def test_completion_returns_video_id(status: int) -> None:
    uploader, _ = _uploader(
        lambda r: httpx.Response(status, json={"id": "abc123", "kind": "youtube#video"})
    )
    assert await uploader.send_chunk(_session(3), 0, b"abc", 3) == UploadCompleted("abc123")


async def test_completion_without_id_is_transient() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(200, json={"kind": "youtube#video"}))
    with pytest.raises(YouTubeTransientError):
        await uploader.send_chunk(_session(3), 0, b"abc", 3)


async def test_query_status_sends_empty_put_with_star_range() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(308, headers={"Range": "bytes=0-99"}))
    assert await uploader.query_status(_session(1000)) == UploadIncomplete(100)
    (req,) = api.requests
    assert req.method == "PUT" and req.headers["Content-Range"] == "bytes */1000"
    assert req.content == b""


@pytest.mark.parametrize("status", [404, 410])
async def test_expired_session(status: int) -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(status))
    assert await uploader.query_status(_session()) == UploadExpired()
    assert await uploader.send_chunk(_session(3), 0, b"abc", 3) == UploadExpired()


@pytest.mark.parametrize(
    ("response", "exc"),
    [
        (_error(403, "quotaExceeded"), YouTubeQuotaError),
        (_error(400, "uploadLimitExceeded"), YouTubeQuotaError),
        (_error(403, "rateLimitExceeded"), YouTubeRateLimitError),
        (httpx.Response(429), YouTubeRateLimitError),
        (_error(403, "forbidden"), YouTubeAuthError),
        (_error(401, "youtubeSignupRequired"), YouTubeAuthError),
        (_error(403, "authorizationRequired"), YouTubeAuthError),
        (_error(400, "invalid_grant"), YouTubeAuthError),
        (httpx.Response(403), YouTubeAuthError),
        (_error(400, "invalidTitle"), YouTubeRejectedError),
        (_error(400, "invalidDescription"), YouTubeRejectedError),
        (_error(400, "invalidTags"), YouTubeRejectedError),
        (_error(403, "forbiddenPrivacySetting"), YouTubeRejectedError),
        (_error(400, "mediaBodyRequired"), YouTubeRejectedError),
        (httpx.Response(400, text="nope"), YouTubeRejectedError),
        (httpx.Response(500), YouTubeTransientError),
        (httpx.Response(503, text=SESSION_URI), YouTubeTransientError),
    ],
)
async def test_error_classification(response: httpx.Response, exc: type[Exception]) -> None:
    for call in ("start", "send", "query"):
        uploader, _ = _uploader(lambda r: response)
        with pytest.raises(exc) as info:
            if call == "start":
                await uploader.start_session(METADATA, 3, "video/mp4")
            elif call == "send":
                await uploader.send_chunk(_session(3), 0, b"abc", 3)
            else:
                await uploader.query_status(_session(3))
        assert UPLOAD_ID not in str(info.value)


@pytest.mark.parametrize(
    "error", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.WriteError]
)
async def test_transport_failures_are_transient_and_redacted(error: type[Exception]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error(f"failed talking to {request.url}")

    uploader, _ = _uploader(handler)
    with pytest.raises(YouTubeTransientError) as info:
        await uploader.send_chunk(_session(3), 0, b"abc", 3)
    assert UPLOAD_ID not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__


async def test_401_refreshes_once_then_succeeds() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["Authorization"])
        if len(seen) == 1:
            return httpx.Response(401)
        return httpx.Response(308, headers={"Range": "bytes=0-2"})

    uploader, api = _uploader(handler)
    assert await uploader.send_chunk(_session(3), 0, b"abc", 3) == UploadIncomplete(3)
    assert seen == ["Bearer at-1", "Bearer at-2"] and api.token_calls == 2


async def test_second_401_is_auth_error() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(401))
    with pytest.raises(YouTubeAuthError):
        await uploader.query_status(_session())
    assert len(api.requests) == 2 and api.token_calls == 2


async def test_invalid_grant_on_refresh_is_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_ENDPOINT:
            return httpx.Response(400, json={"error": "invalid_grant"})
        raise AssertionError("must not reach the API")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = RefreshTokenCredentials("cid", SecretStr("cs"), SecretStr("rt"), client=client)
    uploader = YouTubeResumableUploader(creds, client=client)
    with pytest.raises(YouTubeAuthError, match="invalid_grant"):
        await uploader.start_session(METADATA, 3, "video/mp4")


def test_chunk_size_must_be_multiple_of_256_kib() -> None:
    assert validate_chunk_bytes(8 * 1024 * 1024) == 8 * 1024 * 1024
    for bad in (0, -CHUNK_UNIT_BYTES, CHUNK_UNIT_BYTES + 1, 1000):
        with pytest.raises(ValueError):
            validate_chunk_bytes(bad)
        with pytest.raises(ValueError):
            _uploader(lambda r: httpx.Response(500), chunk_bytes=bad)


async def test_send_chunk_validates_ranges_before_sending() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(500))
    total = CHUNK_UNIT_BYTES * 2
    with pytest.raises(ValueError, match="256 KiB"):
        await uploader.send_chunk(_session(total), 0, b"x" * 1000, total)
    # 256 KiB の倍数でも設定値と違う非最終チャンクは送らない
    big, _ = _uploader(lambda r: httpx.Response(500), chunk_bytes=2 * CHUNK_UNIT_BYTES)
    with pytest.raises(ValueError, match="exactly"):
        await big.send_chunk(
            _session(4 * CHUNK_UNIT_BYTES), 0, b"x" * CHUNK_UNIT_BYTES, 4 * CHUNK_UNIT_BYTES
        )
    with pytest.raises(ValueError):
        await uploader.send_chunk(_session(total), total - 1, b"xx", total)
    with pytest.raises(ValueError):
        await uploader.send_chunk(_session(total), 0, b"x", total + 1)
    with pytest.raises(ValueError):
        await uploader.send_chunk(_session(total), -1, b"x", total)
    assert api.requests == []


async def test_foreign_session_uri_is_never_contacted() -> None:
    uploader, api = _uploader(lambda r: httpx.Response(308))
    bad = UploadSessionRef(uri="https://attacker.example/upload/youtube/v3/videos", total_bytes=3)
    with pytest.raises(YouTubeTransientError):
        await uploader.query_status(bad)
    assert api.requests == [] and api.token_calls == 0


def test_redact() -> None:
    assert UPLOAD_ID not in redact(SESSION_URI)
    assert "upload_id=<redacted>" in redact(SESSION_URI)


def _lookup_handler(pages: list[list[str]], tags: dict[str, list[str]], log: list[str]) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        params = request.url.params
        if url.startswith(f"{API_BASE_URL}/channels"):
            log.append("channels")
            assert params["mine"] == "true" and params["part"] == "contentDetails"
            return httpx.Response(
                200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
            )
        if url.startswith(f"{API_BASE_URL}/playlistItems"):
            index = int(params.get("pageToken", "0"))
            log.append(f"page{index}")
            assert params["playlistId"] == "UUx" and params["maxResults"] == "50"
            body: dict = {"items": [{"contentDetails": {"videoId": v}} for v in pages[index]]}
            if index + 1 < len(pages):
                body["nextPageToken"] = str(index + 1)
            return httpx.Response(200, json=body)
        if url.startswith(f"{API_BASE_URL}/videos"):
            ids = params["id"].split(",")
            log.append(f"videos{len(ids)}")
            assert params["part"] == "snippet"
            return httpx.Response(
                200, json={"items": [{"id": v, "snippet": {"tags": tags.get(v, [])}} for v in ids]}
            )
        raise AssertionError(url)

    return handler


async def test_find_video_by_marker_walks_pages() -> None:
    log: list[str] = []
    pages = [["a", "b"], ["c", "d"]]
    uploader, _ = _uploader(_lookup_handler(pages, {"d": ["x", "avp-marker"]}, log))
    assert await uploader.find_video_by_marker("avp-marker") == "d"
    assert log == ["channels", "page0", "videos2", "page1", "videos2"]


async def test_find_video_by_marker_not_found_and_bounded() -> None:
    log: list[str] = []
    pages = [["v"]] * 10
    uploader, _ = _uploader(_lookup_handler(pages, {}, log), max_lookup_pages=3)
    assert await uploader.find_video_by_marker("avp-marker") is None
    assert log.count("videos1") == 3


async def test_find_video_by_marker_without_channel_is_auth_error() -> None:
    uploader, _ = _uploader(lambda r: httpx.Response(200, json={"items": []}))
    with pytest.raises(YouTubeAuthError):
        await uploader.find_video_by_marker("m")


async def test_find_video_by_marker_quota_error() -> None:
    uploader, _ = _uploader(lambda r: _error(403, "quotaExceeded"))
    with pytest.raises(YouTubeQuotaError):
        await uploader.find_video_by_marker("m")


async def test_forbidden_license_setting_is_a_rejection_not_auth() -> None:
    uploader, _ = _uploader(lambda r: _error(403, "forbiddenLicenseSetting"))
    with pytest.raises(YouTubeRejectedError):
        await uploader.start_session(METADATA, 10, "video/mp4")
