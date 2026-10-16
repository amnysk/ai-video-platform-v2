"""外部呼び出しの観測（provider.call.* / log-contract §2.1・§7.5）。

実ネットワークに出ない（MockTransport）。

理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import json

import httpx
import pytest

from domain.errors import (
    ProviderInputFetchError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
)
from infrastructure.providers.fal_queue import QUEUE_BASE_URL, FalQueueClient, FalSubmission
from infrastructure.providers.fal_storage import STORAGE_TOKEN_URL, FalStorageClient
from tests.support.log_capture import capture_json

ENDPOINT = "vendor/model/v1"
KEY = "0f1e2d3c-4b5a-6978-8a9b-0c1d2e3f4a5b:" + "ab" * 16


def _client(response: httpx.Response) -> FalQueueClient:
    return FalQueueClient(KEY, transport=httpx.MockTransport(lambda _: response))


def _submission() -> FalSubmission:
    base = f"{QUEUE_BASE_URL}/{ENDPOINT}/requests/req-1"
    return FalSubmission(
        endpoint_id=ENDPOINT, request_id="req-1", status_url=f"{base}/status", response_url=base
    )


async def test_403_is_access_denied_from_the_status_alone_and_keeps_its_status() -> None:
    client = _client(httpx.Response(403, headers={"x-fal-request-id": "fal-req-9"}))
    with capture_json() as logs, pytest.raises(ProviderUnavailableError) as caught:
        await client.submit(ENDPOINT, {"prompt": "x"})
    assert caught.value.http_status == 403  # 制御は変えず、観測した status を持たせる
    [failed] = logs.events("provider.call.failed")
    assert failed["provider_operation"] == "submit"
    assert failed["http_status"] == 403
    assert failed["error_category"] == "access_denied"
    assert failed["classification_basis"] == "http_status_only"
    assert failed["provider_request_id"] == "fal-req-9"
    assert failed["provider_endpoint"] == ENDPOINT
    assert KEY not in logs.stream.getvalue()


async def test_422_content_policy_uses_the_provider_error_type() -> None:
    body = {
        "detail": [
            {
                "type": "content_policy_violation",
                "loc": ["body", "prompt"],
                "msg": "flagged",
                "input": "THE FULL PROMPT TEXT",
                "ctx": {"extra_info": {"reason": "partner_validation_failed"}},
            }
        ]
    }
    client = _client(httpx.Response(422, json=body))
    with capture_json() as logs, pytest.raises(ProviderRejectedError):
        await client.submit(ENDPOINT, {"prompt": "x"})
    [failed] = logs.events("provider.call.failed")
    assert failed["error_code"] == ["content_policy_violation"]
    assert failed["error_category"] == "content_policy"
    assert failed["classification_basis"] == "provider_error_type"
    excerpt = json.loads(failed["response_excerpt"])
    assert excerpt["reason"] == "partner_validation_failed"
    assert excerpt["locs"] == ["body.prompt"]
    # 応答全文・入力（prompt）は入れない
    assert "THE FULL PROMPT TEXT" not in json.dumps(excerpt)


async def test_file_download_error_on_result_is_input_unreachable() -> None:
    body = {"detail": [{"type": "file_download_error", "loc": ["body", "image_url"], "msg": "x"}]}
    client = _client(
        httpx.Response(422, json=body, headers={"x-fal-error-type": "file_download_error"})
    )
    with capture_json() as logs, pytest.raises(ProviderInputFetchError):
        await client.result(_submission())
    [failed] = logs.events("provider.call.failed")
    assert failed["provider_operation"] == "result"
    assert failed["error_category"] == "input_unreachable"
    assert failed["error_code"] == ["file_download_error"]
    assert failed["provider_request_id"] == "req-1"


async def test_5xx_submit_is_ambiguous() -> None:
    client = _client(httpx.Response(502))
    with capture_json() as logs, pytest.raises(ProviderSubmitAmbiguousError):
        await client.submit(ENDPOINT, {"prompt": "x"})
    [failed] = logs.events("provider.call.failed")
    assert failed["outcome"] == "ambiguous"
    assert failed["error_category"] == "submit_ambiguous"


async def test_a_poll_is_debug_and_carries_the_raw_status() -> None:
    client = _client(httpx.Response(200, json={"status": "IN_QUEUE"}))
    with capture_json() as logs:
        await client.status(_submission())
    [ok] = logs.events("provider.call.succeeded")
    assert ok["level"] == "DEBUG"
    assert ok["provider_operation"] == "status"
    assert ok["attributes"]["raw_status"] == "IN_QUEUE"


async def test_storage_403_keeps_the_existing_line_and_adds_fields() -> None:
    client = FalStorageClient(
        "secret-key",
        transport=httpx.MockTransport(
            lambda r: (
                httpx.Response(403, headers={"x-fal-request-id": "req-123"})
                if str(r.url) == STORAGE_TOKEN_URL
                else httpx.Response(500)
            )
        ),
    )
    with capture_json() as logs, pytest.raises(ProviderUnavailableError):
        await client.upload(b"PNG", "image/png", "a.png")
    [failed] = logs.events("provider.call.failed")
    assert failed["message"].startswith("PROVIDER_AUTH_FAILURE fal_operation=token http_status=403")
    assert failed["provider"] == "fal_storage"
    assert failed["provider_operation"] == "storage_token"
    assert failed["error_category"] == "access_denied"
    assert failed["provider_request_id"] == "req-123"
    assert "secret-key" not in logs.stream.getvalue()
