"""research Adapter 内部の例外と、domain の失敗クラスへの写像（docs/failure-policy.md §1）。

Adapter は Provider・HTTP の事情で例外を投げるが、Workflow / Activity が見るのは
``domain.errors`` の失敗クラス（型で分類する。メッセージ文字列で判定しない）。写像は
``to_domain_error`` の 1 か所。

domain 側の具体的な型名（``ResearchUrlNotAllowedError`` など）は別ブランチで追加される。
ここでは**遅延 import** で解決し、無ければ基底クラス（``PermanentError`` など）へ落とす。
どちらでも失敗の**分類**（retryable / needs_input / permanent）は変わらない。

例外メッセージに URL の userinfo・token・API キーを入れない。
"""

from __future__ import annotations

import importlib

from domain.errors import DomainError, NeedsInputError, PermanentError, RetryableError
from domain.research.ports import FetchedContent


class ResearchAdapterError(Exception):
    """research Adapter の例外の基底。"""


class UrlNotAllowed(ResearchAdapterError):  # noqa: N818 - 仕様上の名前
    """SSRF 規則に違反した URL（scheme・userinfo・接続先 IP・名前）。同じ入力では必ず同じ結果。"""

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or f"url not allowed: {reason}")
        self.reason = reason


class ResolutionError(ResearchAdapterError):
    """名前解決に失敗した（NXDOMAIN・応答なし・不正な応答）。ネットワーク障害として再試行できる。"""


class ResearchProviderNotConfigured(ResearchAdapterError):  # noqa: N818
    """実 Provider が未選定・未設定。人間が選ぶまで実行しない（ADR-0036 §3）。"""


class ProviderRateLimited(ResearchAdapterError):  # noqa: N818
    """429 相当。backoff の後で再試行できる。"""


class ProviderTransient(ResearchAdapterError):  # noqa: N818
    """5xx・通信失敗・読めない応答。再試行できる。"""


class ProviderQuotaExhausted(ResearchAdapterError):  # noqa: N818
    """quota・上限の枯渇。時間が経つか人間が上限を上げるまで直らない。"""


class ProviderAuthRejected(ResearchAdapterError):  # noqa: N818
    """認証・認可が人手でしか直らない（token 失効・scope 不足）。"""


class ProviderRejected(ResearchAdapterError):  # noqa: N818
    """入力の拒否（400 系）。同じ入力での再試行は無意味。"""


def _domain_type(name: str, fallback: type[DomainError]) -> type[DomainError]:
    """``domain.errors`` にその型があれば使い、無ければ基底へ落とす（遅延 import）。"""
    module = importlib.import_module("domain.errors")
    candidate = getattr(module, name, None)
    if isinstance(candidate, type) and issubclass(candidate, fallback):
        return candidate
    return fallback


def fetch_failure_to_domain_error(content: FetchedContent) -> DomainError | None:
    """失敗した取得を domain の失敗クラスへ。成功・切り詰めは ``None``（例外にしない）。

    - ``blocked_url`` / ``bad_content_type`` / ``too_large`` / 4xx（429 以外）: ``permanent``
      （同じ URL・同じ資料では同じ結果。取得できないだけで、依頼全体の失敗にするかは呼び出し側）
    - ``timeout`` / ``network`` / ``http_5xx`` / 429: ``retryable``
    """
    if content.fetch_status != "failed":
        return None
    kind = content.error
    detail = content.error_detail or kind or "unknown"
    if kind == "blocked_url":
        cls = _domain_type("ResearchUrlNotAllowedError", PermanentError)
        return cls(f"fetch blocked: {detail}")
    if kind in ("timeout", "network", "http_5xx") or (
        kind == "http_4xx" and content.status_code == 429
    ):
        return RetryableError(f"fetch failed (retryable): {kind} {detail}")
    return PermanentError(f"fetch failed: {kind} {detail}")


def to_domain_error(source: BaseException | FetchedContent) -> DomainError:
    """Adapter の例外または失敗した取得を、domain の失敗クラスへ写像する。"""
    if isinstance(source, FetchedContent):
        mapped = fetch_failure_to_domain_error(source)
        if mapped is None:
            raise ValueError("a successful fetch is not a failure")
        return mapped
    if isinstance(source, DomainError):
        return source
    if isinstance(source, UrlNotAllowed):
        return _domain_type("ResearchUrlNotAllowedError", PermanentError)(str(source))
    if isinstance(source, ResearchProviderNotConfigured):
        return _domain_type("ResearchProviderNotConfiguredError", NeedsInputError)(str(source))
    if isinstance(source, (ProviderQuotaExhausted, ProviderAuthRejected)):
        return NeedsInputError(str(source))
    if isinstance(source, (ProviderRateLimited, ProviderTransient, ResolutionError)):
        return RetryableError(str(source))
    if isinstance(source, ProviderRejected):
        return PermanentError(str(source))
    youtube = _youtube_error_to_domain(source)
    if youtube is not None:
        return youtube
    raise source  # 未知の例外は分類しない（握りつぶさない）


def _youtube_error_to_domain(exc: BaseException) -> DomainError | None:
    """YouTube adapter の例外（``infrastructure/youtube/errors.py``）の写像。"""
    errors = importlib.import_module("infrastructure.youtube.errors")
    if isinstance(exc, (errors.YouTubeQuotaError, errors.YouTubeAuthError)):
        return NeedsInputError(str(exc))
    if isinstance(exc, (errors.YouTubeRateLimitError, errors.YouTubeTransientError)):
        return RetryableError(str(exc))
    if isinstance(exc, errors.YouTubeRejectedError):
        return PermanentError(str(exc))
    return None
