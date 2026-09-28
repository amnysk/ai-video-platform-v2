"""fal の queue API を叩く provider 中立な HTTP クライアント（ADR-0017）。

モデル固有の payload は各 adapter（``fal_seedream_image`` 等）が組む。ここは
submit / status / result / download と、**結果の分類**だけを持つ。

分類（呼び出し側 = 予約台帳の意味論に直結する）:

- **受理されなかったことが確実**（接続前に失敗 / 429 / 4xx）
  - 一時的（接続拒否・DNS・429・``X-Fal-Retryable``）→ ``ProviderJobFailedError``（retryable）
  - 拒否（入力不正・ポリシー）→ ``ProviderRejectedError``（needs_input）
  - 認証（401/403）→ ``ProviderUnavailableError``（needs_input）
- **受理されたか分からない**（送信後のタイムアウト / 5xx / request_id 欠落 / 想定外の HTTP 例外）
  → ``ProviderSubmitAmbiguousError``。fal に冪等キーは無いので**再送しない**
- **2xx + request_id は受理**。以後の異常（URL のホスト違い・型の崩れ）で「受理されなかった」に
  しない。参照を返して台帳に記録させ、poll 時にホストを検査する（``UnreconciledReservationError``）
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
    ProviderRejection,
    ProviderSubmitAmbiguousError,
    ProviderUnavailableError,
    TransientError,
    UnreconciledReservationError,
)
from domain.production.ports import ProviderJobRef

logger = logging.getLogger(__name__)

QUEUE_BASE_URL = "https://queue.fal.run"
REF_VERSION = 1
#: 生の取得物の上限（正規化前。Artifact の上限とは別）。
DEFAULT_DOWNLOAD_MAX_BYTES = 64 * 1024 * 1024
#: status / result / download の読み取り待ち（heartbeat timeout 90秒より十分短く）
DEFAULT_READ_TIMEOUT_SECONDS = 30.0

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
        """台帳の参照を読む。読めない参照は人手照合（課金ジョブの所在が分からない）。"""
        try:
            data = json.loads(ref)
            if data.get("v") != REF_VERSION:
                raise ValueError(f"unsupported ref version {data.get('v')!r}")
            fields = {k: data[k] for k in ("endpoint", "request_id", "status_url", "response_url")}
            if not all(isinstance(v, str) and v for v in fields.values()):
                raise ValueError("ref fields must be non-empty strings")
            cancel_url = data.get("cancel_url")
            return cls(
                endpoint_id=fields["endpoint"],
                request_id=fields["request_id"],
                status_url=fields["status_url"],
                response_url=fields["response_url"],
                cancel_url=cancel_url if isinstance(cancel_url, str) else None,
            )
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise UnreconciledReservationError(f"unreadable fal job ref: {exc}") from exc


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


#: 例外メッセージ・error_summary に残す要約の上限。DB の error_summary は 2000 文字まで
#: 許容するが（``jobs.error_summary`` / ``provider_reservations.error_summary``）、workflow
#: 側の ``_summary()``（``workers/production/workflows.py``）がさらに 1000 文字で切る。
#: そこに収まりながら、拒否理由（``msg`` / ``ctx.extra_info.reason``）を単語の途中で
#: 切らないだけの余裕を持たせる（ADR-0034: 2026-09-26/27 の 422 事故は、旧 500 文字の
#: 素朴な str()+slice が構造化 body を丸ごと文字列化してから切っていたため、実際に残った
#: 拒否理由が584文字で単語途中に切れていた）。
_SHORT_MAX_CHARS = 800


def _short(body: Any) -> str:
    """例外メッセージ用の要約。URL を含む巨大な body をそのまま載せない。

    fal の 422 は多くの場合 ``{"detail": [{"type": ..., "msg": ..., "loc": [...], "ctx": {...}}]}``
    という pydantic 形式の validation error リストを返す。素朴に ``str(body["detail"])`` して
    切ると、Python の repr 表現ごと途中の単語で切れ、拒否理由（``msg``）が読めなくなる
    （実際に起きた事故）。構造化されていれば type/msg/loc/reason を人が読める形に整形してから
    切る。整形できない形は今まで通り素朴に文字列化する。
    """
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, list) and detail:
            formatted = "; ".join(_format_detail_item(item) for item in detail)
            if formatted:
                return formatted[:_SHORT_MAX_CHARS]
        for key in ("error", "message", "detail"):
            if key in body:
                return str(body[key])[:_SHORT_MAX_CHARS]
    return str(body)[:_SHORT_MAX_CHARS]


def _format_detail_item(item: Any) -> str:
    """1件の validation error を ``type: msg (at loc) [reason]`` に整形する。"""
    if not isinstance(item, dict):
        return str(item)
    type_ = item.get("type")
    msg = item.get("msg")
    piece = ": ".join(str(p) for p in (type_, msg) if p)
    loc = item.get("loc")
    if isinstance(loc, list) and loc:
        location = ".".join(str(p) for p in loc)
        piece = f"{piece} (at {location})" if piece else f"(at {location})"
    reason = _extra_info_reason(item.get("ctx"))
    if reason:
        piece = f"{piece} [{reason}]" if piece else f"[{reason}]"
    return piece or str(item)


def _extra_info_reason(ctx: Any) -> str | None:
    if not isinstance(ctx, dict):
        return None
    extra_info = ctx.get("extra_info")
    if not isinstance(extra_info, dict):
        return None
    reason = extra_info.get("reason")
    return reason if isinstance(reason, str) and reason else None


#: 構造化した拒否の ``message`` の上限（``provider_rejections.message`` と揃える）。
_REJECTION_MESSAGE_MAX_CHARS = 1000


def _rejection(body: Any, types: list[str], status: int) -> ProviderRejection:
    """422 等の応答から構造化した拒否を組み立てる（ADR-0035）。文字列からは推測しない。

    ``detail[]`` の ``loc``（``["body", "image_url"]`` → ``"body.image_url"``）、
    ``msg``、``ctx.extra_info.reason`` を読む。body が構造化されていなければ位置・理由は空。
    """
    locs: list[str] = []
    reasons: list[str] = []
    messages: list[str] = []
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        for item in detail:
            if not isinstance(item, dict):
                continue
            loc = item.get("loc")
            if isinstance(loc, list) and loc:
                locs.append(".".join(str(part) for part in loc))
            reason = _extra_info_reason(item.get("ctx"))
            if reason:
                reasons.append(reason)
            msg = item.get("msg")
            if isinstance(msg, str) and msg:
                messages.append(msg)
    message = "; ".join(messages)[:_REJECTION_MESSAGE_MAX_CHARS] or None
    return ProviderRejection(
        types=tuple(dict.fromkeys(types)),
        locs=tuple(dict.fromkeys(locs)),
        reason=reasons[0] if reasons else None,
        message=message,
        http_status=status,
    )


def _json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _str_or[T](value: Any, default: T) -> str | T:
    return value if isinstance(value, str) and value else default


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
        read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """``read_timeout_seconds`` は status / result / download の1回の読み取り待ち。

        await Activity の heartbeat timeout（90秒）より十分短くする（1回の待ちで heartbeat を
        途切れさせない）。submit だけは ``timeout_seconds`` を使う（送信後の読み取りタイムアウトは
        曖昧になるので、窓を短くしない）。
        """
        if not api_key:
            raise ProviderUnavailableError("FAL_KEY is not configured")
        self._base_url = base_url.rstrip("/")
        self._submit_timeout = httpx.Timeout(timeout_seconds, connect=connect_timeout_seconds)
        timeout = httpx.Timeout(
            timeout_seconds, connect=connect_timeout_seconds, read=read_timeout_seconds
        )
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
            response = await self._api.post(url, json=payload, timeout=self._submit_timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # 送信前に失敗した（接続拒否・DNS・TLS 前）。受理されていない。
            raise ProviderJobFailedError(
                f"fal submit not accepted (connection failed): {type(exc).__name__}"
            ) from exc
        except httpx.HTTPError as exc:
            # 送信後のタイムアウト・切断・想定外の HTTP 例外。受理されたか分からない。
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
                f"fal submit rejected: HTTP {status} types={types}: {_short(body)}",
                rejection=_rejection(body, types, status),
            )
        if not isinstance(body, dict) or not body.get("request_id"):
            raise ProviderSubmitAmbiguousError(
                f"fal submit returned HTTP {status} without request_id"
            )
        # ここから先は**受理済み**。何があっても参照を返し、台帳に記録させる。
        # URL のホスト検査は poll 時（_check_queue_url → UnreconciledReservationError）。
        request_id = str(body["request_id"])
        base = f"{url}/requests/{request_id}"
        submission = FalSubmission(
            endpoint_id=endpoint_id,
            request_id=request_id,
            status_url=_str_or(body.get("status_url"), f"{base}/status"),
            response_url=_str_or(body.get("response_url"), base),
            cancel_url=_str_or(body.get("cancel_url"), None),
        )
        if not (
            submission.status_url.startswith(self._base_url + "/")
            and submission.response_url.startswith(self._base_url + "/")
        ):
            logger.warning(
                "fal submit accepted with off-host job urls; recording ref, poll will refuse "
                "request_id=%s",
                request_id,
            )
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
            raise ProviderRejectedError(summary, rejection=_rejection(body, types, status))
        if _retryable_header(response) is True:
            raise ProviderJobFailedError(summary)
        if status == 422 or any(t in REJECTED_ERROR_TYPES for t in types):
            raise ProviderRejectedError(summary, rejection=_rejection(body, types, status))
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
            # 課金済みかもしれないジョブの参照が信用できない。再生成せず人手照合へ
            raise UnreconciledReservationError(
                "fal job url does not point at the configured queue host"
            )


__all__ = [
    "CONTENT_POLICY_ERROR_TYPE",
    "DEFAULT_DOWNLOAD_MAX_BYTES",
    "DEFAULT_READ_TIMEOUT_SECONDS",
    "QUEUE_BASE_URL",
    "FalQueueClient",
    "FalQueueState",
    "FalStatus",
    "FalSubmission",
]
