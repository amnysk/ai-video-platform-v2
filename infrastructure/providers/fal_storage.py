"""fal CDN v3 へのファイルアップロード（ADR-0017 Phase 4C）。**非課金の準備工程**。

手順（fal-client 1.0.1 ``client.py`` / ``auth.py`` のソースで確認）:

1. ``POST https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3``、body ``{}``、
   ``Authorization: Key <FAL_KEY>`` → ``{token, token_type, base_url, expires_at}``
2. ``POST https://v3.fal.media/files/upload``、生バイト、
   ``Authorization: <token_type> <token>`` / ``Content-Type`` / ``X-Fal-File-Name`` /
   ``X-Fal-Object-Lifecycle: {"expiration_duration_seconds": N}`` → ``{access_url}``

課金されないので、失敗は予約台帳の意味論と無関係（呼び出し側は予約**前**に実行する）:

- 認証（401/403）→ ``ProviderUnavailableError``（needs_input）
- 通信失敗 / 429 / 5xx / 応答の形が不正 → ``ProviderInvocationError``（retryable）
- その他の 4xx（サイズ超過など）→ ``ProviderRejectedError``（needs_input）

API キーと一時トークンはログ・例外メッセージに出さない。
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx

from domain.errors import (
    ProviderInvocationError,
    ProviderRejectedError,
    ProviderUnavailableError,
)

STORAGE_TOKEN_URL = "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3"
CDN_UPLOAD_URL = "https://v3.fal.media/files/upload"
#: 生成の submit 直前に上げるので 1 日で足りる（provider は受理時に取得する）。
DEFAULT_LIFECYCLE_SECONDS = 24 * 60 * 60


def _raise_for_status(response: httpx.Response, *, what: str) -> None:
    status = response.status_code
    if status < 400:
        return
    if status in (401, 403):
        raise ProviderUnavailableError(f"fal storage {what} refused: HTTP {status} (credentials)")
    if status == 429 or status >= 500:
        raise ProviderInvocationError(f"fal storage {what} failed: HTTP {status}")
    raise ProviderRejectedError(f"fal storage {what} rejected: HTTP {status}")


def _json_object(response: httpx.Response, *, what: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise ProviderInvocationError(f"fal storage {what} returned non-JSON") from exc
    if not isinstance(body, dict):
        raise ProviderInvocationError(f"fal storage {what} returned a non-object body")
    return body


class FalStorageClient:
    """``transport`` はテストで ``httpx.MockTransport`` を渡す。"""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 10.0,
        lifecycle_seconds: int = DEFAULT_LIFECYCLE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailableError("FAL_KEY is not configured")
        self._api_key = api_key
        self._lifecycle_seconds = lifecycle_seconds
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds),
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def upload(self, data: bytes, content_type: str, file_name: str) -> str:
        """バイト列を上げて、provider へ渡せる https の ``access_url`` を返す。"""
        if not data:
            raise ProviderRejectedError("fal storage upload of empty data")
        token_type, token = await self._token()
        headers = {
            "Authorization": f"{token_type} {token}",
            "Content-Type": content_type,
            "X-Fal-File-Name": file_name,
            "X-Fal-Object-Lifecycle": json.dumps(
                {"expiration_duration_seconds": self._lifecycle_seconds}
            ),
        }
        response = await self._post(CDN_UPLOAD_URL, what="upload", content=data, headers=headers)
        _raise_for_status(response, what="upload")
        url = _json_object(response, what="upload").get("access_url")
        if not isinstance(url, str) or urlparse(url).scheme != "https":
            raise ProviderInvocationError("fal storage upload returned no https access_url")
        return url

    async def _token(self) -> tuple[str, str]:
        response = await self._post(
            STORAGE_TOKEN_URL,
            what="token",
            json={},
            headers={
                "Authorization": f"Key {self._api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        _raise_for_status(response, what="token")
        body = _json_object(response, what="token")
        token, token_type = body.get("token"), body.get("token_type")
        if not isinstance(token, str) or not token or not isinstance(token_type, str):
            raise ProviderInvocationError("fal storage token response is missing fields")
        return token_type, token

    async def _post(self, url: str, *, what: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.post(url, **kwargs)
        except httpx.TransportError as exc:
            raise ProviderInvocationError(
                f"fal storage {what} request failed: {type(exc).__name__}"
            ) from exc


__all__ = ["CDN_UPLOAD_URL", "DEFAULT_LIFECYCLE_SECONDS", "STORAGE_TOKEN_URL", "FalStorageClient"]
