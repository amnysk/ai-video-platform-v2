"""外部呼び出し1回の観測（``provider.call.*`` / log-contract §2.1・§7.5）。

adapter（``FalQueueClient``・``FalStorageClient``・YouTube）が、自分が観測した事実
（``http_status``・許可したヘッダ・provider のエラー種別）だけを足して出す。``provider``・
``reservation_id`` 等は呼び出し側（``PaidJobRunner``）が文脈に束縛している。

``error_category`` は**推定**でログ専用。既存の再試行・422 fallback・needs_input・課金の制御は
例外の型で決まっていて、ここは何も変えない。HTTP 403 だけなら ``access_denied`` /
``http_status_only``（credentials とは断定しない）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from contracts.log_contract import (
    ClassificationBasis,
    ErrorCategory,
    EventName,
    Outcome,
    ProviderOperation,
)
from domain.errors import (
    MediaValidationError,
    ProviderJobFailedError,
    ProviderSubmitAmbiguousError,
    TransientError,
    UnreconciledReservationError,
)
from infrastructure.logging.emit import emit

#: ``response_excerpt`` に入れてよい応答ヘッダ（log-contract §7.5）
ALLOWED_RESPONSE_HEADERS: tuple[str, ...] = (
    "x-fal-retryable",
    "x-fal-error-type",
    "x-fal-request-id",
    "retry-after",
    "content-type",
)


def allowed_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    if not headers:
        return {}
    out: dict[str, str] = {}
    for name in ALLOWED_RESPONSE_HEADERS:
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value:
            out[name] = str(value)[:200]
    return out


def classify(
    exc: BaseException | None, http_status: int | None, headers: Mapping[str, str] | None = None
) -> tuple[str, str]:
    """``(error_category, classification_basis)``。観測できた根拠の強い順に見る。"""
    rejection = getattr(exc, "rejection", None)
    if rejection is not None and getattr(rejection, "category", None) is not None:
        basis = (
            ClassificationBasis.PROVIDER_ERROR_TYPE
            if getattr(rejection, "types", ())
            else ClassificationBasis.HTTP_STATUS_ONLY
        )
        return rejection.category.value, basis.value
    if http_status == 401:
        return ErrorCategory.AUTH_REJECTED.value, ClassificationBasis.HTTP_STATUS_ONLY.value
    if http_status == 403:
        return ErrorCategory.ACCESS_DENIED.value, ClassificationBasis.HTTP_STATUS_ONLY.value
    if http_status == 429:
        return ErrorCategory.RATE_LIMITED.value, ClassificationBasis.HTTP_STATUS_ONLY.value
    retryable = (headers or {}).get("x-fal-retryable") if headers is not None else None
    if retryable is not None and str(retryable).strip().lower() == "true":
        return ErrorCategory.PROVIDER_UNAVAILABLE.value, ClassificationBasis.PROVIDER_HEADER.value
    if isinstance(exc, ProviderSubmitAmbiguousError):
        return ErrorCategory.SUBMIT_AMBIGUOUS.value, ClassificationBasis.EXCEPTION_TYPE.value
    if http_status is not None and http_status >= 500:
        return ErrorCategory.PROVIDER_UNAVAILABLE.value, ClassificationBasis.HTTP_STATUS_ONLY.value
    if isinstance(exc, TimeoutError) or "Timeout" in type(exc).__name__:
        return ErrorCategory.TIMEOUT.value, ClassificationBasis.EXCEPTION_TYPE.value
    if isinstance(exc, MediaValidationError):
        return ErrorCategory.MEDIA_VALIDATION.value, ClassificationBasis.EXCEPTION_TYPE.value
    if isinstance(exc, UnreconciledReservationError):
        return (
            ErrorCategory.UNRECONCILED_RESERVATION.value,
            ClassificationBasis.EXCEPTION_TYPE.value,
        )
    if isinstance(exc, TransientError) and http_status is None:
        return ErrorCategory.TRANSIENT_NETWORK.value, ClassificationBasis.EXCEPTION_TYPE.value
    if isinstance(exc, ProviderJobFailedError):
        return ErrorCategory.PROVIDER_JOB_FAILED.value, ClassificationBasis.EXCEPTION_TYPE.value
    return ErrorCategory.UNKNOWN.value, ClassificationBasis.NONE.value


class CallObservation:
    """1回の呼び出しで adapter が観測したもの。終わったら ``succeeded`` / ``failed`` を1度呼ぶ。"""

    def __init__(
        self,
        logger: logging.Logger,
        operation: ProviderOperation,
        *,
        provider: str | None = None,
        endpoint: str | None = None,
    ) -> None:
        self.logger = logger
        self.operation = operation
        self.provider = provider
        self.endpoint = endpoint
        self.http_status: int | None = None
        self.headers: Mapping[str, str] | None = None
        self.error_codes: list[str] = []
        self.provider_request_id: str | None = None
        self._started = time.monotonic()

    def response(self, status: int, headers: Mapping[str, str] | None) -> None:
        self.http_status = status
        self.headers = headers
        try:
            request_id = headers.get("x-fal-request-id") if headers is not None else None
            if request_id and self.provider_request_id is None:
                self.provider_request_id = str(request_id)
            header_type = headers.get("x-fal-error-type") if headers is not None else None
            if header_type and header_type not in self.error_codes:
                self.error_codes.append(str(header_type))
        except Exception:
            pass

    def _base(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_operation": self.operation.value,
            "provider_endpoint": self.endpoint,
            "provider_request_id": self.provider_request_id,
            "http_status": self.http_status,
            "duration_ms": (time.monotonic() - self._started) * 1000,
        }

    def succeeded(self, level: int = logging.INFO, **fields: Any) -> None:
        emit(
            self.logger,
            EventName.PROVIDER_CALL_SUCCEEDED,
            level,
            "provider %s succeeded",
            self.operation.value,
            **{**self._base(), "outcome": Outcome.SUCCEEDED.value, **fields},
        )

    def failed(self, exc: BaseException, level: int = logging.WARNING, **fields: Any) -> None:
        """例外を観測として記録する（例外そのものは呼び出し側がそのまま再送出する）。"""
        try:
            rejection = getattr(exc, "rejection", None)
            codes = list(self.error_codes)
            for t in getattr(rejection, "types", ()) or ():
                if t not in codes:
                    codes.append(t)
            category, basis = classify(exc, self.http_status, self.headers)
            excerpt: dict[str, Any] = {}
            headers = allowed_headers(self.headers)
            if headers:
                excerpt["headers"] = headers
            if rejection is not None:
                excerpt.update(
                    types=list(getattr(rejection, "types", ()) or ()),
                    locs=list(getattr(rejection, "locs", ()) or ()),
                    reason=getattr(rejection, "reason", None),
                    message=getattr(rejection, "message", None),
                )
            ambiguous = isinstance(exc, ProviderSubmitAmbiguousError)
            emit(
                self.logger,
                EventName.PROVIDER_CALL_FAILED,
                level,
                "provider %s failed: %s",
                self.operation.value,
                type(exc).__name__,
                **{
                    **self._base(),
                    "outcome": (Outcome.AMBIGUOUS if ambiguous else Outcome.FAILED).value,
                    "error_type": type(exc).__name__,
                    "error_code": codes or None,
                    "error_category": category,
                    "classification_basis": basis,
                    "error_message": str(exc),
                    "response_excerpt": excerpt or None,
                    **fields,
                },
            )
        except Exception:  # ログの故障は業務へ伝播させない（INV-38）
            pass


__all__ = ["ALLOWED_RESPONSE_HEADERS", "CallObservation", "allowed_headers", "classify"]
