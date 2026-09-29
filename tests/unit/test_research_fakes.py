"""Fake Provider と固定コーパス（ADR-0036）。

Fake は通常テストの唯一の Provider であり、worker の Fake 実行でも使う。ここが壊れると
下流（Evidence / Trend）のテストが「もっともらしいが偽の入力」で緑になるので、
コーパスの**性質**（転載・snippet だけ・異説・量化子など）と決定性、
実ネットワークに出ないことを固定する。
"""

from __future__ import annotations

import hashlib
import socket
from datetime import UTC, datetime

import pytest

from domain.research.ports import ContentFetcher, SearchProvider, SearchQuery
from infrastructure.research import fake_corpus as c
from infrastructure.research.errors import (
    ProviderRateLimited,
    ResearchProviderNotConfigured,
    to_domain_error,
)
from infrastructure.research.fake_providers import FIXED_NOW, FakeContentFetcher, FakeSearchProvider
from infrastructure.research.not_configured import NotConfiguredSearchProvider
from infrastructure.research.quota_costs import YOUTUBE_FULL_SEARCH_UNITS


def _q(text: str, kind: str = "web", n: int = 10, **kw: object) -> SearchQuery:
    return SearchQuery(text=text, kind=kind, max_results=n, **kw)  # type: ignore[arg-type]


def test_fakes_satisfy_the_ports() -> None:
    provider: SearchProvider = FakeSearchProvider()
    fetcher: ContentFetcher = FakeContentFetcher()
    assert provider.name == "fake" and fetcher is not None


# --- 検索 ------------------------------------------------------------------------------


async def test_search_is_deterministic_and_ranked_by_matches_then_url() -> None:
    provider = FakeSearchProvider()
    first = await provider.search(_q("関ヶ原 1600 徳川家康"))
    second = await FakeSearchProvider().search(_q("関ヶ原 1600 徳川家康"))
    assert first == second
    urls = [h.url for h in first.hits]
    assert urls[0] == c.SEKIGAHARA_A  # 一致が最も多い
    assert set(urls) >= {c.SEKIGAHARA_A, c.SEKIGAHARA_B, c.SEKIGAHARA_COPY}
    assert first.provider == "fake" and first.cost_units == 0 and first.searched_at == FIXED_NOW


async def test_search_respects_max_results_and_reports_truncation() -> None:
    results = await FakeSearchProvider().search(_q("関ヶ原", n=2))
    assert len(results.hits) == 2 and results.truncated is True
    everything = await FakeSearchProvider().search(_q("関ヶ原", n=50))
    assert everything.truncated is False


async def test_search_without_matches_returns_no_hits() -> None:
    results = await FakeSearchProvider().search(_q("存在しない話題zzz"))
    assert results.hits == () and results.truncated is False


async def test_web_and_youtube_queries_use_separate_documents() -> None:
    provider = FakeSearchProvider()
    web = await provider.search(_q("関ヶ原"))
    yt = await provider.search(_q("関ヶ原", kind="youtube"))
    assert all(h.url.startswith("https://www.youtube.com/") for h in yt.hits) and yt.hits
    assert not any(h.url.startswith("https://www.youtube.com/") for h in web.hits)


async def test_youtube_results_carry_observation_time_and_quota_units() -> None:
    results = await FakeSearchProvider().search(_q("関ヶ原", kind="youtube"))
    assert results.cost_units == YOUTUBE_FULL_SEARCH_UNITS == 102
    hit = next(h for h in results.hits if h.url == c.YT_SEKIGAHARA_LONG)
    assert hit.provider_ref == "sekigahara01" and hit.channel_subscriber_count == 88_000
    assert hit.duration_seconds == 612
    assert {s.metric for s in hit.stats} == {"view_count", "like_count", "comment_count"}
    assert all(s.observed_at == c.OBSERVED_AT for s in hit.stats)


async def test_missing_observations_stay_missing() -> None:
    """隠された購読者数・コメント数を 0 にしない（不明は不明）。"""
    results = await FakeSearchProvider().search(_q("関ヶ原", kind="youtube"))
    hit = next(h for h in results.hits if h.url == c.YT_SEKIGAHARA_SHORT)
    assert hit.channel_subscriber_count is None
    assert "comment_count" not in {s.metric for s in hit.stats}


async def test_region_and_language_are_recorded_as_what_was_requested_only() -> None:
    results = await FakeSearchProvider().search(
        _q("関ヶ原", kind="youtube", region_code="US", language="en")
    )
    for hit in results.hits:
        assert hit.extras == {
            "requested_region_code": "US",
            "requested_relevance_language": "en",
        }
    plain = await FakeSearchProvider().search(_q("関ヶ原", kind="youtube"))
    assert all(h.extras == {} for h in plain.hits)


async def test_published_filters_apply() -> None:
    after = datetime(2026, 8, 20, tzinfo=UTC)
    results = await FakeSearchProvider().search(_q("関ヶ原", kind="youtube", published_after=after))
    assert [h.url for h in results.hits] == [c.YT_SEKIGAHARA_SHORT]


async def test_calls_are_recorded_including_failed_ones() -> None:
    provider = FakeSearchProvider()
    provider.fail_with_429()
    with pytest.raises(ProviderRateLimited):
        await provider.search(_q("関ヶ原"))
    await provider.search(_q("鉄砲"))
    assert [q.text for q in provider.calls] == ["関ヶ原", "鉄砲"]


async def test_fail_next_injects_any_error_and_is_consumed_in_order() -> None:
    provider = FakeSearchProvider()
    provider.fail_next(RuntimeError("boom"), times=2)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await provider.search(_q("関ヶ原"))
    assert (await provider.search(_q("関ヶ原"))).hits


async def test_a_429_is_classified_as_retryable() -> None:
    from domain.errors import RetryableError

    provider = FakeSearchProvider()
    provider.fail_with_429()
    with pytest.raises(ProviderRateLimited) as caught:
        await provider.search(_q("関ヶ原"))
    assert isinstance(to_domain_error(caught.value), RetryableError)


async def test_an_invalid_query_is_rejected() -> None:
    with pytest.raises(ValueError):
        await FakeSearchProvider().search(_q("関ヶ原", n=0))


# --- 本文取得 --------------------------------------------------------------------------


async def test_fetch_returns_the_body_with_its_hash() -> None:
    result = await FakeContentFetcher().fetch(c.SEKIGAHARA_A)
    assert result.body_confirmed and result.final_url == c.SEKIGAHARA_A
    assert result.text == c.SEKIGAHARA_BODY
    assert result.content_sha256 == hashlib.sha256(c.SEKIGAHARA_BODY.encode()).hexdigest()
    assert result.fetched_at == FIXED_NOW


async def test_a_reprinted_document_has_the_same_hash_but_is_another_url() -> None:
    """独立性の検査用: 別 URL でも本文が同一なら 1 つの源として数える材料になる。"""
    fetcher = FakeContentFetcher()
    original = await fetcher.fetch(c.SEKIGAHARA_A)
    copy = await fetcher.fetch(c.SEKIGAHARA_COPY)
    other = await fetcher.fetch(c.SEKIGAHARA_B)
    assert original.content_sha256 == copy.content_sha256
    assert original.content_sha256 != other.content_sha256
    assert original.final_url != copy.final_url


async def test_a_snippet_only_document_cannot_be_fetched() -> None:
    results = await FakeSearchProvider().search(_q("明治維新 財政"))
    hit = next(h for h in results.hits if h.url == c.MEIJI_SNIPPET_ONLY)
    assert "1868" in hit.snippet  # snippet には年があるが…
    fetched = await FakeContentFetcher().fetch(hit.url)
    assert fetched.fetch_status == "failed" and fetched.error == "http_4xx"
    assert fetched.status_code == 403 and not fetched.body_confirmed  # …本文確認済みにならない


@pytest.mark.parametrize(
    ("url", "status", "error"),
    [
        (c.TIMEOUT_URL, "failed", "timeout"),
        (c.PDF_URL, "failed", "bad_content_type"),
        (c.UNKNOWN_URL, "failed", "http_4xx"),
        (c.TRUNCATED_URL, "truncated", "too_large"),
    ],
)
async def test_unusable_documents_are_never_confirmed(url: str, status: str, error: str) -> None:
    result = await FakeContentFetcher().fetch(url)
    assert (result.fetch_status, result.error) == (status, error)
    assert not result.body_confirmed


async def test_a_redirecting_url_reports_the_final_url() -> None:
    result = await FakeContentFetcher().fetch(c.SEKIGAHARA_SHORT_LINK)
    assert result.requested_url == c.SEKIGAHARA_SHORT_LINK
    assert result.final_url == c.SEKIGAHARA_A
    assert result.redirect_chain == (c.SEKIGAHARA_SHORT_LINK,) and result.body_confirmed


async def test_fetch_failure_injection_and_history() -> None:
    fetcher = FakeContentFetcher()
    fetcher.fail_next_with("http_5xx", 503)
    injected = await fetcher.fetch(c.SEKIGAHARA_A)
    assert injected.error == "http_5xx" and injected.status_code == 503
    assert (await fetcher.fetch(c.SEKIGAHARA_A)).body_confirmed
    fetcher.fail_next(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await fetcher.fetch(c.SEKIGAHARA_A)
    assert fetcher.calls == [c.SEKIGAHARA_A] * 3


# --- コーパスの性質（下流の難しいケースを持っていること） -----------------------------------


def test_the_corpus_contains_each_hard_case() -> None:
    corpus = c.default_corpus()
    by = {d.url: d for d in corpus.documents}
    assert by[c.SEKIGAHARA_A].body == by[c.SEKIGAHARA_COPY].body  # 転載
    assert by[c.MEIJI_SNIPPET_ONLY].body is None  # snippet だけ
    assert "1600" in by[c.SEKIGAHARA_A].body  # type: ignore[operator]
    assert "1543" in by[c.TANEGASHIMA_A].body and "1542" in by[c.TANEGASHIMA_DISSENT].body  # type: ignore[operator]
    assert "1868" in by[c.MEIJI_A].body and "1858" in by[c.MEIJI_WRONG_YEAR].body  # type: ignore[operator]
    assert "一部" in by[c.GUN_ADOPTION_SOME].body and "すべて" in by[c.GUN_ADOPTION_ALL].body  # type: ignore[operator]
    assert "一部" in by[c.SEKIGAHARA_SOME].body and "すべて" in by[c.SEKIGAHARA_ALL].body  # type: ignore[operator]
    causal = ("ため", "により", "よって", "原因", "結果", "おかげ", "から")
    assert not any(w in by[c.NO_CAUSATION].body for w in causal)  # type: ignore[operator]
    assert len({d.url for d in corpus.documents}) == len(corpus.documents)


def test_every_document_is_documented_and_urls_are_reserved_example_hosts() -> None:
    for doc in c.default_corpus().documents:
        if doc.kind == "web":
            assert ".example/" in doc.url, doc.url  # 実在のサイトを名乗らない
        assert doc.title and doc.snippet


# --- 実ネットワークに出ない -----------------------------------------------------------------


async def test_fakes_never_touch_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("a fake provider must not use the network")

    for name in ("getaddrinfo", "gethostbyname", "create_connection"):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    provider, fetcher = FakeSearchProvider(), FakeContentFetcher()
    await provider.search(_q("関ヶ原"))
    await provider.search(_q("関ヶ原", kind="youtube"))
    for url in (c.SEKIGAHARA_A, c.TIMEOUT_URL, c.SEKIGAHARA_SHORT_LINK, c.UNKNOWN_URL):
        await fetcher.fetch(url)


# --- 未設定の Web 検索 ------------------------------------------------------------------------


async def test_the_unconfigured_web_provider_refuses_to_search() -> None:
    from domain.errors import NeedsInputError

    provider: SearchProvider = NotConfiguredSearchProvider()
    assert provider.name == "web"
    with pytest.raises(ResearchProviderNotConfigured) as caught:
        await provider.search(_q("関ヶ原"))
    mapped = to_domain_error(caught.value)
    assert isinstance(mapped, NeedsInputError)  # 依頼は blocked。retry しない
