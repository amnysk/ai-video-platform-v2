"""fal queue クライアントの結果分類（ADR-0017）。実ネットワークに出ない（MockTransport）。"""

from __future__ import annotations

import json

import httpx
import pytest

from domain.errors import (
    MediaValidationError,
    ProviderJobFailedError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
    TransientError,
    UnreconciledReservationError,
)
from infrastructure.providers.fal_queue import (
    QUEUE_BASE_URL,
    FalQueueClient,
    FalQueueState,
    FalSubmission,
)

ENDPOINT = "vendor/model/v1"
KEY = "secret-key-123"


def _client(handler) -> FalQueueClient:
    return FalQueueClient(KEY, transport=httpx.MockTransport(handler))


def _submission() -> FalSubmission:
    base = f"{QUEUE_BASE_URL}/{ENDPOINT}/requests/req-1"
    return FalSubmission(
        endpoint_id=ENDPOINT, request_id="req-1", status_url=f"{base}/status", response_url=base
    )


def _accepted(request: httpx.Request) -> httpx.Response:
    base = f"{QUEUE_BASE_URL}/{ENDPOINT}/requests/req-1"
    return httpx.Response(
        200,
        json={
            "request_id": "req-1",
            "status_url": f"{base}/status",
            "response_url": base,
            "cancel_url": f"{base}/cancel",
            "queue_position": 0,
        },
    )


async def test_submit_sends_key_header_and_returns_resumable_ref() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _accepted(request)

    client = _client(handler)
    submission = await client.submit(ENDPOINT, {"prompt": "x"})
    assert seen[0].headers["authorization"] == f"Key {KEY}"
    assert str(seen[0].url) == f"{QUEUE_BASE_URL}/{ENDPOINT}"
    assert json.loads(seen[0].content) == {"prompt": "x"}
    ref = submission.to_ref()
    assert KEY not in ref
    assert FalSubmission.from_ref(ref) == submission


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, json={"detail": "rate"}), ProviderJobFailedError),
        (httpx.Response(401, json={"detail": "no"}), ProviderUnavailableError),
        (httpx.Response(403), ProviderUnavailableError),
        (
            httpx.Response(422, json={"detail": [{"type": "value_error", "msg": "bad"}]}),
            ProviderRejectedError,
        ),
        (
            httpx.Response(
                422, json={"detail": [{"type": "content_policy_violation", "msg": "nsfw"}]}
            ),
            ProviderRejectedError,
        ),
        (httpx.Response(400, headers={"x-fal-retryable": "true"}), ProviderJobFailedError),
        (httpx.Response(500, text="boom"), ProviderSubmitAmbiguousError),
        (httpx.Response(503), ProviderSubmitAmbiguousError),
        (httpx.Response(200, json={"status": "IN_QUEUE"}), ProviderSubmitAmbiguousError),
        (httpx.Response(200, text="not json"), ProviderSubmitAmbiguousError),
    ],
)
async def test_submit_http_outcomes_are_classified(response, error) -> None:
    client = _client(lambda request: response)
    with pytest.raises(error) as info:
        await client.submit(ENDPOINT, {"prompt": "x"})
    assert KEY not in str(info.value)


@pytest.mark.parametrize(
    ("exc", "error"),
    [
        (httpx.ConnectError("refused"), ProviderJobFailedError),
        (httpx.ConnectTimeout("dns"), ProviderJobFailedError),
        (httpx.ReadTimeout("slow"), ProviderSubmitAmbiguousError),
        (httpx.WriteTimeout("slow"), ProviderSubmitAmbiguousError),
        (httpx.RemoteProtocolError("closed"), ProviderSubmitAmbiguousError),
    ],
)
async def test_submit_transport_failures_split_not_accepted_from_ambiguous(exc, error) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    with pytest.raises(error):
        await _client(handler).submit(ENDPOINT, {"prompt": "x"})


async def test_submit_accepted_with_off_host_urls_still_returns_the_ref() -> None:
    """2xx + request_id は受理。URL の異常で「受理されなかった」にしない（台帳に参照を残す）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "r",
                "status_url": "https://evil.example/status",
                "response_url": "https://evil.example/r",
            },
        )

    submission = await _client(handler).submit(ENDPOINT, {})
    assert submission.request_id == "r"
    assert FalSubmission.from_ref(submission.to_ref()).status_url == "https://evil.example/status"


@pytest.mark.parametrize(
    "body",
    [
        {"request_id": "r", "status_url": 12, "response_url": ["x"]},
        {"request_id": "r", "cancel_url": {"nested": True}},
    ],
)
async def test_submit_accepted_with_odd_fields_never_reports_not_accepted(body) -> None:
    submission = await _client(lambda r: httpx.Response(200, json=body)).submit(ENDPOINT, {})
    assert submission.request_id == "r"


async def test_submit_unexpected_http_error_after_send_is_ambiguous() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError("bad gzip")

    with pytest.raises(ProviderSubmitAmbiguousError):
        await _client(handler).submit(ENDPOINT, {})


@pytest.mark.parametrize("what", ["status", "result"])
async def test_poll_off_host_ref_is_unreconciled_and_never_sends_the_key(what) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "COMPLETED"})

    evil = FalSubmission(
        endpoint_id=ENDPOINT,
        request_id="r",
        status_url="https://evil.example/status",
        response_url="https://evil.example/r",
    )
    with pytest.raises(UnreconciledReservationError):
        await getattr(_client(handler), what)(evil)
    assert seen == []


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("IN_QUEUE", FalQueueState.PENDING),
        ("IN_PROGRESS", FalQueueState.PENDING),
        ("COMPLETED", FalQueueState.COMPLETED),
    ],
)
async def test_status_maps_queue_states(status, state) -> None:
    client = _client(lambda request: httpx.Response(200, json={"status": status}))
    assert (await client.status(_submission())).state is state


@pytest.mark.parametrize(
    "response", [httpx.Response(500), httpx.Response(429), httpx.Response(404)]
)
async def test_status_http_errors_are_transient(response) -> None:
    with pytest.raises(TransientError):
        await _client(lambda request: response).status(_submission())


async def test_status_network_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(TransientError):
        await _client(handler).status(_submission())


async def test_result_success_returns_body() -> None:
    body = {"images": [{"url": "https://cdn.example/a.png"}], "seed": 1}
    client = _client(lambda request: httpx.Response(200, json=body))
    assert await client.result(_submission()) == body


@pytest.mark.parametrize(
    ("response", "error"),
    [
        # COMPLETED でも HTTP 200 の body に error がある
        (
            httpx.Response(200, json={"error": "runner died", "error_type": "runner_server_error"}),
            ProviderJobFailedError,
        ),
        (
            httpx.Response(200, json={"error": "x", "error_type": "content_policy_violation"}),
            ProviderRejectedError,
        ),
        (
            httpx.Response(422, json={"detail": [{"type": "content_policy_violation"}]}),
            ProviderRejectedError,
        ),
        (
            httpx.Response(422, json={"detail": [{"type": "no_media_generated"}]}),
            ProviderRejectedError,
        ),
        (
            httpx.Response(504, json={"detail": "timeout", "error_type": "generation_timeout"}),
            ProviderJobFailedError,
        ),
        (
            httpx.Response(
                422, json={"detail": [{"type": "value_error"}]}, headers={"x-fal-retryable": "true"}
            ),
            ProviderJobFailedError,
        ),
        (
            httpx.Response(500, headers={"x-fal-error-type": "downstream_service_error"}),
            ProviderJobFailedError,
        ),
        (httpx.Response(502), TransientError),
    ],
)
async def test_result_failures_are_classified(response, error) -> None:
    with pytest.raises(error):
        await _client(lambda request: response).result(_submission())


async def test_download_streams_without_api_key_and_enforces_cap() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"x" * 100)

    client = _client(handler)
    chunks: list[bytes] = []

    async def write(chunk: bytes) -> None:
        chunks.append(chunk)

    assert await client.download("https://cdn.example/a.png", write) == 100
    assert b"".join(chunks) == b"x" * 100
    assert "authorization" not in seen[0].headers
    with pytest.raises(MediaValidationError):
        await client.download("https://cdn.example/a.png", write, max_bytes=10)


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404), ProviderJobFailedError),
        (httpx.Response(503), TransientError),
        (httpx.Response(200, content=b""), ProviderJobFailedError),
    ],
)
async def test_download_failures(response, error) -> None:
    async def write(chunk: bytes) -> None:
        pass

    with pytest.raises(error):
        await _client(lambda request: response).download("https://cdn.example/a", write)


async def test_download_rejects_non_https() -> None:
    async def write(chunk: bytes) -> None:
        pass

    with pytest.raises(ProviderJobFailedError):
        await _client(lambda r: httpx.Response(200)).download("http://cdn.example/a", write)


@pytest.mark.parametrize(
    "ref", ["fake-job-1", '{"v": 9}', '{"v": 1, "endpoint": "e"}', "[]", '{"v": 1, "endpoint": "e", '
    '"request_id": "r", "status_url": 1, "response_url": "u"}']
)
def test_unreadable_ref_is_unreconciled(ref) -> None:
    with pytest.raises(UnreconciledReservationError):
        FalSubmission.from_ref(ref)


def test_missing_key_refuses_to_build() -> None:
    with pytest.raises(ProviderUnavailableError):
        FalQueueClient("")
