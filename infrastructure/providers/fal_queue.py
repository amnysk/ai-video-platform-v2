"""fal の queue API を叩く provider 中立な HTTP クライアント（ADR-0017）。

モデル固有の payload は各 adapter（``fal_seedream_image`` 等）が組む。ここは
submit / status / result / download と、**結果の分類**だけを持つ。

分類（呼び出し側 = 予約台帳の意味論に直結する）:

- **受理されなかったことが確実**（接続前に失敗 / 429 / 4xx）
  - 一時的（接続拒否・DNS・429・``X-Fal-Retryable``）→ ``ProviderJobFailedError``（retryable）
  - 拒否（入力不正・ポリシー）→ ``ProviderRejectedError``（needs_input）
  - 認証（401/403）→ ``ProviderUnavailableError``（needs_input）
- **受理されたか分からない**（送信後のタイムアウト / 5xx / request_id 欠落）
  → ``ProviderSubmitAmbiguousError``。fal に冪等キーは無いので**再送しない**
- status / result / download の通信失敗は参照に対して冪等なので ``TransientError``

secret（API キー）はログ・例外メッセージに出さない。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

import httpx

from domain.errors import (
    MediaValidationError,
    ProviderJobFailedError,
    ProviderRejectedError,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
    TransientError,
)
from domain.production.ports import ProviderJobRef

logger = logging.getLogger(__name__)

QUEUE_BASE_URL = "https://queue.fal.run"
REF_VERSION = 1
#: 生の取得物の上限（正規化前。Artifact の上限とは別）。
DEFAULT_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024

#: 人間が入力を直せば回復する拒否（docs: 422 detail[].type）。
REJECTED_ERROR_TYPES = frozenset(
    {
        "content_policy_violation",
        "image_too_large",
        "image_too_small",
        "image_load_error",
        "file_download_error",
        "file_too_large",
        "face_detection_error",
        "no_media_generated",
        "value_error",
        "unsupported_image_format",
        "unsupported_audio_format",
        "unsupported_video_format",
        "sequence_too_long",
        "sequence_too_short",
        "one_of",
        "greater_than",
        "less_than",
        "missing",
        "invalid_archive",
    }
)
CONTENT_POLICY_ERROR_TYPE = "content_policy_violation"


class FalQueueState(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class FalSubmission:
    endpoint_id: str
    request_id: str
    status_url: str
    response_url: str
    cancel_url: str | None = None

    def to_ref(self) -> ProviderJobRef:
        """台帳に保存する不透明な参照（再開に必要なものだけ。secret は含まない）。"""
        return ProviderJobRef(
            json.dumps(
                {
                    "v": REF_VERSION,
                    "endpoint": self.endpoint_id,
                    "request_id": self.request_id,
                    "status_url": self.status_url,
                    "response_url": self.response_url,
                    "cancel_url": self.cancel_url,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )

    @classmethod
    def from_ref(cls, ref: str) -> FalSubmission:
        try:
            data = json.loads(ref)
            if data.get("v") != REF_VERSION:
                raise ValueError(f"unsupported ref version {data.get('v')!r}")
            return cls(
                endpoint_id=str(data["endpoint"]),
                request_id=str(data["request_id"]),
                status_url=str(data["status_url"]),
                response_url=str(data["response_url"]),
                cancel_url=data.get("cancel_url"),
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ProviderJobFailedError(f"unreadable fal job ref: {exc}") from exc


@dataclass(frozen=True, slots=True)
class FalStatus:
    state: FalQueueState
    raw_status: str


def _error_types_from_body(body: Any) -> list[str]:
    types: list[str] = []
    if isinstance(body, dict):
        if isinstance(body.get("error_type"), str):
            types.append(body["error_type"])
        detail = body.get("detail")
        if isinstance(detail, list):
            for item in detail:
                if isinstance(item, dict) and isinstance(item.get("type"), str):
                    types.append(item["type"])
    return types


def _short(body: Any) -> str:
    """例外メッセージ用の要約。URL を含む巨大な body をそのまま載せない。"""
    if isinstance(body, dict):
        for key in ("error", "detail", "message"):
            if key in body:
                return str(body[key])[:500]
    return str(body)[:300]


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _retryable_header(response: httpx.Response) -> bool | None:
    value = response.headers.get("x-fal-retryable")
    if value is None:
        return None
    return value.strip().lower() == "true"


class FalQueueClient:
    """fal queue の HTTP クライアント。``transport`` はテストで ``httpx.MockTransport`` を渡す。"""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = QUEUE_BASE_URL,
        timeout_seconds: float = 120.0,
        connect_timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ProviderUnavailableError("FAL_KEY is not configured")
        self._base_url = base_url.rstrip("/")
        timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        self._api = httpx.AsyncClient(
            headers={"Authorization": f"Key {api_key}"},
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
        )
        # CDN の取得には API キーを送らない（別ホストへ secret を渡さない）
        self._cdn = httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True)

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._cdn.aclose()

    # ------------------------------------------------------------------ submit

    async def submit(self, endpoint_id: str, payload: dict[str, Any]) -> FalSubmission:
        url = f"{self._base_url}/{endpoint_id.strip('/')}"
        try:
            response = await self._api.post(url, json=payload)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # 送信前に失敗した（接続拒否・DNS・TLS 前）。受理されていない。
            raise ProviderJobFailedError(
                f"fal submit not accepted (connection failed): {type(exc).__name__}"
            ) from exc
        except httpx.TransportError as exc:
            # 送信後のタイムアウト・切断。受理されたか分からない。
            raise ProviderSubmitAmbiguousError(
                f"fal submit outcome unknown: {type(exc).__name__}"
            ) from exc

        status = response.status_code
        body = _json_or_none(response)
        if status >= 500:
            raise ProviderSubmitAmbiguousError(f"fal submit outcome unknown: HTTP {status}")
        if status == 429:
            raise ProviderJobFailedError("fal submit not accepted: HTTP 429 (rate limited)")
        if status in (401, 403):
            raise ProviderUnavailableError(f"fal submit refused: HTTP {status} (credentials)")
        if status >= 400:
            if _retryable_header(response) is True:
                raise ProviderJobFailedError(
                    f"fal submit not accepted (retryable): HTTP {status}: {_short(body)}"
                )
            types = _error_types_from_body(body)
            raise ProviderRejectedError(
                f"fal submit rejected: HTTP {status} types={types}: {_short(body)}"
            )
        if not isinstance(body, dict) or not body.get("request_id"):
            raise ProviderSubmitAmbiguousError(
                f"fal submit returned HTTP {status} without request_id"
            )
        request_id = str(body["request_id"])
        base = f"{url}/requests/{request_id}"
        submission = FalSubmission(
            endpoint_id=endpoint_id,
            request_id=request_id,
            status_url=str(body.get("status_url") or f"{base}/status"),
            response_url=str(body.get("response_url") or base),
            cancel_url=body.get("cancel_url"),
        )
        self._check_queue_url(submission.status_url)
        self._check_queue_url(submission.response_url)
        return submission

    # ------------------------------------------------------------------ status / result

    async def status(self, submission: FalSubmission) -> FalStatus:
        self._check_queue_url(submission.status_url)
        response = await self._get(self._api, submission.status_url, what="status")
        body = _json_or_none(response)
        if response.status_code >= 400:
            self._raise_poll_http_error(response, body, what="status")
        raw = str(body.get("status", "")) if isinstance(body, dict) else ""
        if raw in ("IN_QUEUE", "IN_PROGRESS"):
            return FalStatus(state=FalQueueState.PENDING, raw_status=raw)
        if raw == "COMPLETED":
            # COMPLETED は成功とは限らない。判定は result() が body で行う。
            return FalStatus(state=FalQueueState.COMPLETED, raw_status=raw)
        raise TransientError(f"fal status returned unexpected status {raw!r}")

    async def result(self, submission: FalSubmission) -> dict[str, Any]:
        """完了したジョブの結果。**HTTP 200 でも body の error を検査する**。"""
        self._check_queue_url(submission.response_url)
        response = await self._get(self._api, submission.response_url, what="result")
        body = _json_or_none(response)
        types = _error_types_from_body(body)
        header_type = response.headers.get("x-fal-error-type")
        if header_type:
            types.append(header_type)
        has_error = bool(types) or (isinstance(body, dict) and body.get("error") is not None)
        status = response.status_code

        if not has_error and status < 400:
            if not isinstance(body, dict):
                raise TransientError("fal result body is not a JSON object")
            return body
        if not has_error:
            self._raise_poll_http_error(response, body, what="result")

        summary = f"fal job failed: HTTP {status} types={types}: {_short(body)}"
        if CONTENT_POLICY_ERROR_TYPE in types:
            raise ProviderRejectedError(summary)
        if _retryable_header(response) is True:
            raise ProviderJobFailedError(summary)
        if status == 422 or any(t in REJECTED_ERROR_TYPES for t in types):
            raise ProviderRejectedError(summary)
        # runner / timeout / downstream / 未知の error_type
        raise ProviderJobFailedError(summary)

    # ------------------------------------------------------------------ download

    async def download(
        self,
        url: str,
        write: Callable[[bytes], Awaitable[None]],
        *,
        max_bytes: int = DEFAULT_DOWNLOAD_MAX_BYTES,
    ) -> int:
        """ストリーミングで取得して ``write`` へ渡す。上限超過は ``MediaValidationError``。"""
        if urlparse(url).scheme != "https":
            raise ProviderJobFailedError("fal media url must be https")
        total = 0
        try:
            async with self._cdn.stream("GET", url) as response:
                if response.status_code >= 400:
                    if response.status_code in (404, 410):
                        raise ProviderJobFailedError(
                            f"fal media no longer available: HTTP {response.status_code}"
                        )
                    raise TransientError(f"fal media download failed: HTTP {response.status_code}")
                length = response.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > max_bytes:
                    raise MediaValidationError(
                        f"fal media is {length} bytes, above the {max_bytes} byte cap"
                    )
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise MediaValidationError(
                            f"fal media exceeded the {max_bytes} byte cap while downloading"
                        )
                    await write(chunk)
        except httpx.TransportError as exc:
            raise TransientError(f"fal media download failed: {type(exc).__name__}") from exc
        if total == 0:
            raise ProviderJobFailedError("fal media download was empty")
        return total

    # ------------------------------------------------------------------ internal

    async def _get(self, client: httpx.AsyncClient, url: str, *, what: str) -> httpx.Response:
        try:
            return await client.get(url)
        except httpx.TransportError as exc:
            raise TransientError(f"fal {what} request failed: {type(exc).__name__}") from exc

    @staticmethod
    def _raise_poll_http_error(response: httpx.Response, body: Any, *, what: str) -> None:
        status = response.status_code
        if status in (401, 403):
            raise ProviderUnavailableError(f"fal {what} refused: HTTP {status} (credentials)")
        # 参照に対する GET は冪等。429 / 5xx / その他は再 await で回復しうる。
        raise TransientError(f"fal {what} failed: HTTP {status}: {_short(body)}")

    def _check_queue_url(self, url: str) -> None:
        """台帳の参照から URL を読むので、queue のホスト以外へ API キーを送らない。"""
        if not url.startswith(self._base_url + "/"):
            raise ProviderJobFailedError("fal job url does not point at the configured queue host")


__all__ = [
    "CONTENT_POLICY_ERROR_TYPE",
    "DEFAULT_DOWNLOAD_MAX_BYTES",
    "QUEUE_BASE_URL",
    "FalQueueClient",
    "FalQueueState",
    "FalStatus",
    "FalSubmission",
]
