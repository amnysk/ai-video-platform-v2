"""fal の 401/402/403 の分類と本文要約（ADR-0057 / INV-71）。実ネットワークに出ない。"""

from __future__ import annotations

import httpx
import pytest

from contracts.provider_health import RefusalKind
from domain.errors import (
    ProviderBalanceExhaustedError,
    ProviderRefusalError,
    ProviderRefusalUnknownError,
    ProviderUnavailableError,
)
from infrastructure.providers.fal_queue import FalQueueClient
from infrastructure.providers.fal_refusal import (
    BALANCE_BODY_MARKERS,
    classify_refusal,
    summarize_refusal_body,
)

ENDPOINT = "vendor/model/v1"
FAKE_KEY = "00000000-aaaa-bbbb-cccc-111111111111:fakesecretfakesecretfakesecret12"


def test_balance_marker_beats_status_for_402_and_403_and_401() -> None:
    body = {"detail": "User is locked. Reason: Exhausted balance. Add credits."}
    for status in (401, 402, 403):
        assert classify_refusal(status, body).kind is RefusalKind.BALANCE_EXHAUSTED


def test_status_alone_never_decides_balance() -> None:
    # 402 / 403 でも文書化された印が本文に無ければ確定しない（安全側 = UNKNOWN）
    assert classify_refusal(402, {"detail": "payment required"}).kind is RefusalKind.UNKNOWN
    assert classify_refusal(403, None).kind is RefusalKind.UNKNOWN
    assert classify_refusal(403, {"detail": "forbidden"}).kind is RefusalKind.UNKNOWN


def test_401_without_balance_marker_is_auth_invalid() -> None:
    result = classify_refusal(401, {"detail": "Invalid authentication credentials"})
    assert result.kind is RefusalKind.AUTH_INVALID


def test_three_statuses_map_to_three_different_kinds() -> None:
    kinds = {
        classify_refusal(401, {"detail": "bad key"}).kind,
        classify_refusal(402, {"detail": "User is locked: Exhausted balance"}).kind,
        classify_refusal(403, {"detail": "nope"}).kind,
    }
    assert kinds == {
        RefusalKind.AUTH_INVALID,
        RefusalKind.BALANCE_EXHAUSTED,
        RefusalKind.UNKNOWN,
    }


def test_markers_live_in_one_place_with_provenance() -> None:
    assert BALANCE_BODY_MARKERS, "marker table must not be empty"
    for marker in BALANCE_BODY_MARKERS:
        assert marker.text == marker.text.lower()
        assert marker.source.startswith("https://") or marker.source.startswith("observed:")


def test_structured_and_list_bodies_are_searched() -> None:
    listed = {"detail": [{"type": "x", "msg": "Exhausted balance"}]}
    assert classify_refusal(403, listed).kind is RefusalKind.BALANCE_EXHAUSTED
    nested = {"error": {"message": "user is locked"}}
    assert classify_refusal(403, nested).kind is RefusalKind.BALANCE_EXHAUSTED


def test_summary_redacts_secrets_urls_and_truncates() -> None:
    body = {
        "detail": (
            f"User is locked: Exhausted balance. key={FAKE_KEY} Authorization: Key {FAKE_KEY} "
            "see https://v3.fal.media/files/x.png?token=SECRETTOKEN&X-Amz-Signature=SIG123 "
            + "z"
            * 2000
        )
    }
    summary = summarize_refusal_body(body, secrets=(FAKE_KEY,))
    assert FAKE_KEY not in summary
    assert "fakesecretfakesecret" not in summary
    assert "SECRETTOKEN" not in summary and "SIG123" not in summary and "?" not in summary
    assert "Exhausted balance" in summary
    assert len(summary) <= 400


def test_summary_of_unreadable_body_is_explicit() -> None:
    assert summarize_refusal_body(None) == "(no response body)"


def _client(response: httpx.Response) -> FalQueueClient:
    return FalQueueClient(FAKE_KEY, transport=httpx.MockTransport(lambda r: response))


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (
            httpx.Response(403, json={"detail": "User is locked: Exhausted balance"}),
            ProviderBalanceExhaustedError,
        ),
        (httpx.Response(402, json={"detail": "Exhausted balance"}), ProviderBalanceExhaustedError),
        (httpx.Response(402, json={"detail": "pay"}), ProviderRefusalUnknownError),
        (httpx.Response(403, json={"detail": "forbidden"}), ProviderRefusalUnknownError),
        (httpx.Response(403), ProviderRefusalUnknownError),
        (httpx.Response(401, json={"detail": "bad key"}), ProviderUnavailableError),
    ],
)
async def test_submit_refusals_use_distinct_error_types(response, error) -> None:
    with pytest.raises(error) as info:
        await _client(response).submit(ENDPOINT, {"prompt": "x"})
    assert FAKE_KEY not in str(info.value)
    if issubclass(error, ProviderRefusalError):
        # 認証 incident（ADR-0030）に数えない・入力拒否ではない
        assert not isinstance(info.value, ProviderUnavailableError)
        assert info.value.http_status == response.status_code


async def test_401_diagnostic_body_is_kept_in_the_message() -> None:
    with pytest.raises(ProviderUnavailableError) as info:
        await _client(httpx.Response(401, json={"detail": "bad key"})).submit(ENDPOINT, {})
    assert "bad key" in str(info.value)
    assert info.value.http_status == 401
