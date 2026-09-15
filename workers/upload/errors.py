"""YouTube adapter の例外 → ドメインの失敗クラス（ADR-0020 §11）。

domain は adapter を知らないので、写像は worker 側に置く。
メッセージは adapter が redact 済みだが、既知の session URI を念のため伏せる（INV-20）。
"""

from __future__ import annotations

from collections.abc import Iterable

from domain.errors import (
    DomainError,
    TransientError,
    UploadAuthError,
    UploadQuotaExceededError,
    UploadRejectedError,
)
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeRejectedError,
    YouTubeSessionExpiredError,
    YouTubeTransientError,
)
from infrastructure.youtube.uploader import redact

REDACTED = "<redacted>"


def scrub(text: str, secrets: Iterable[str | None] = ()) -> str:
    """URL query の token と、渡された session URI を伏せる。"""
    cleaned = redact(text)
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, REDACTED)
    return cleaned


def translate_youtube_error(
    exc: BaseException, secrets: Iterable[str | None] = ()
) -> BaseException:
    """adapter の例外をドメイン例外へ。該当しなければ ``exc`` をそのまま返す。

    ``YouTubeSessionExpiredError`` は通常 port が ``UploadExpired`` で返す。例外で来たら
    一時障害として扱い、再実行が予約行の状態（dispatched か）から判断する。
    """
    if isinstance(exc, DomainError) or not isinstance(exc, YouTubeError):
        return exc
    message = scrub(f"{type(exc).__name__}: {exc}", tuple(secrets))
    mapped: DomainError
    if isinstance(exc, YouTubeAuthError):
        mapped = UploadAuthError(message)
    elif isinstance(exc, YouTubeQuotaError | YouTubeRateLimitError):
        mapped = UploadQuotaExceededError(message)
    elif isinstance(exc, YouTubeRejectedError):
        mapped = UploadRejectedError(message)
    elif isinstance(exc, YouTubeTransientError | YouTubeSessionExpiredError):
        mapped = TransientError(message)
    else:
        mapped = TransientError(message)
    return mapped


__all__ = ["REDACTED", "scrub", "translate_youtube_error"]
