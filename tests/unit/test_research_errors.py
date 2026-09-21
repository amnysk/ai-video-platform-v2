"""Adapter の失敗 → domain の失敗クラス（docs/failure-policy.md §1）。

分類は例外の**型**で行う。domain 側の具体名（``ResearchUrlNotAllowedError`` 等）は別ブランチで
追加されるので、ここでは基底クラス（PermanentError / RetryableError / NeedsInputError）への
isinstance だけを検査する。具体型が存在すればそのサブクラスになるだけで、分類は変わらない。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domain.errors import NeedsInputError, PermanentError, RetryableError
from domain.research.ports import FetchedContent
from infrastructure.research.errors import (
    ProviderAuthRejected,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRejected,
    ProviderTransient,
    ResearchProviderNotConfigured,
    ResolutionError,
    UrlNotAllowed,
    to_domain_error,
)


def _failed(kind: str, status: int | None = None) -> FetchedContent:
    return FetchedContent(
        requested_url="https://a.example/",
        final_url="https://a.example/",
        redirect_chain=(),
        status_code=status,
        content_type=None,
        text=None,
        content_sha256=None,
        bytes_read=0,
        truncated=False,
        fetched_at=datetime(2026, 9, 21, tzinfo=UTC),
        fetch_status="failed",
        error=kind,  # type: ignore[arg-type]
        error_detail="x",
    )


@pytest.mark.parametrize(
    ("kind", "status", "expected"),
    [
        ("blocked_url", None, PermanentError),
        ("bad_content_type", 200, PermanentError),
        ("too_large", 200, PermanentError),
        ("http_4xx", 404, PermanentError),
        ("http_4xx", 403, PermanentError),
        ("http_4xx", 429, RetryableError),
        ("http_5xx", 503, RetryableError),
        ("timeout", None, RetryableError),
        ("network", None, RetryableError),
    ],
)
def test_failed_fetches_are_classified_by_kind(
    kind: str, status: int | None, expected: type[Exception]
) -> None:
    error = to_domain_error(_failed(kind, status))
    assert isinstance(error, expected)


def test_a_url_policy_violation_is_permanent() -> None:
    assert isinstance(to_domain_error(UrlNotAllowed("blocked_ip")), PermanentError)


def test_a_missing_provider_is_needs_input() -> None:
    assert isinstance(to_domain_error(ResearchProviderNotConfigured("x")), NeedsInputError)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ProviderRateLimited("429"), RetryableError),
        (ProviderTransient("5xx"), RetryableError),
        (ResolutionError("dns"), RetryableError),
        (ProviderQuotaExhausted("quota"), NeedsInputError),
        (ProviderAuthRejected("401"), NeedsInputError),
        (ProviderRejected("400"), PermanentError),
    ],
)
def test_provider_errors_map_to_their_failure_class(
    exc: Exception, expected: type[Exception]
) -> None:
    assert isinstance(to_domain_error(exc), expected)


def test_youtube_adapter_errors_map_to_their_failure_class() -> None:
    from infrastructure.youtube.errors import (
        YouTubeAuthError,
        YouTubeQuotaError,
        YouTubeRateLimitError,
        YouTubeRejectedError,
        YouTubeTransientError,
    )

    assert isinstance(to_domain_error(YouTubeQuotaError("q")), NeedsInputError)
    assert isinstance(to_domain_error(YouTubeAuthError("a")), NeedsInputError)
    assert isinstance(to_domain_error(YouTubeRateLimitError("r")), RetryableError)
    assert isinstance(to_domain_error(YouTubeTransientError("t")), RetryableError)
    assert isinstance(to_domain_error(YouTubeRejectedError("x")), PermanentError)


def test_unknown_exceptions_are_not_swallowed() -> None:
    with pytest.raises(KeyError):
        to_domain_error(KeyError("bug"))


def test_a_successful_fetch_is_not_a_failure() -> None:
    ok = _failed("network")
    object.__setattr__(ok, "fetch_status", "fetched")
    with pytest.raises(ValueError):
        to_domain_error(ok)
