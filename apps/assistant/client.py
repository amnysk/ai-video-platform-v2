"""介入 API への HTTP クライアント（httpx）。token は Authorization ヘッダにだけ載せる。

token・ヘッダ・本文を例外やログに出さない。全ての要求に相関 ID と tool 名を付ける（ADR-0042 §6）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import httpx

from contracts.assistant_tools import CORRELATION_HEADER, TOOL_HEADER, correlation_id

API = "/v1/intervention"
DEFAULT_TIMEOUT_SECONDS = 30.0
#: ``CommandEnvelope.correlation_id`` の最大長（contracts.intervention）。超えるときは短縮する
ENVELOPE_CORRELATION_MAX = 64


class AssistantTransportError(Exception):
    """応答を得られなかった（接続・timeout）。送信が受理されたかは分からない。"""


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status: int
    #: JSON なら parse 済み、そうでなければ本文の文字列
    body: Any

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def error(self) -> str | None:
        detail = self.body.get("detail") if isinstance(self.body, dict) else None
        if isinstance(detail, dict):
            value = detail.get("error")
            return value if isinstance(value, str) else None
        return None


def envelope_correlation_id(session_id: str, tool_call_id: str) -> str:
    """command に保存する相関 ID。ヘッダの値が長ければ、同じ入力から決まる短縮形にする。"""
    full = correlation_id(session_id, tool_call_id)
    if len(full) <= ENVELOPE_CORRELATION_MAX:
        return full
    return "assistant:" + hashlib.sha256(full.encode("utf-8")).hexdigest()[:32]


class InterventionClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        session_id: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not token:
            raise ValueError("an intervention token is required")
        self._token = token
        self.session_id = session_id
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), transport=transport, timeout=timeout
        )

    def __repr__(self) -> str:  # token を表示しない
        return f"InterventionClient(session_id={self.session_id!r})"

    def close(self) -> None:
        self._http.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        tool: str,
        tool_call_id: str,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> ApiResponse:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json, text/markdown;q=0.9",
            CORRELATION_HEADER: correlation_id(self.session_id, tool_call_id),
            TOOL_HEADER: tool,
        }
        try:
            response = self._http.request(
                method, API + path, params=params, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            # 例外の文言は使わない（URL・ヘッダを含みうる）。型名だけ
            raise AssistantTransportError(type(exc).__name__) from None
        try:
            body: Any = response.json()
        except ValueError:
            body = response.text
        return ApiResponse(status=response.status_code, body=body)
