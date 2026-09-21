"""research Port の値オブジェクト（ADR-0031 §7）。

Port は「取得できた事実」と「本文を確認できたか」を分けて運ぶ。取得失敗・切り詰め・
テキスト化できない資料を、呼び出し側が誤って「本文確認済み」として扱わないための形を固定する。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from domain.research.ports import (
    ContentFetcher,
    FetchedContent,
    SearchHit,
    SearchProvider,
    SearchQuery,
    SearchResults,
    StatObservation,
)

NOW = datetime(2026, 9, 21, tzinfo=UTC)


def _content(**overrides: object) -> FetchedContent:
    base: dict[str, object] = {
        "requested_url": "https://a.example/x",
        "final_url": "https://a.example/x",
        "redirect_chain": (),
        "status_code": 200,
        "content_type": "text/html",
        "text": "本文",
        "content_sha256": "0" * 64,
        "bytes_read": 6,
        "truncated": False,
        "fetched_at": NOW,
        "fetch_status": "fetched",
        "error": None,
        "error_detail": None,
    }
    base.update(overrides)
    return FetchedContent(**base)  # type: ignore[arg-type]


def test_a_complete_text_fetch_counts_as_a_confirmed_body() -> None:
    assert _content().body_confirmed is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetch_status": "truncated", "truncated": True},
        {"fetch_status": "failed", "error": "timeout", "text": None},
        {"text": None},
        {"text": ""},
        {"text": "  \n\t "},
        {"truncated": True},
    ],
    ids=["truncated", "failed", "no_text", "empty", "blank", "truncated_flag_only"],
)
def test_only_a_complete_nonblank_text_is_a_confirmed_body(overrides: dict[str, object]) -> None:
    """切り詰め・失敗・テキスト無しを「本文確認済み」にしない（ADR-0031 §7）。"""
    assert _content(**overrides).body_confirmed is False


def test_value_objects_are_frozen() -> None:
    query = SearchQuery(text="関ヶ原", kind="web", max_results=5)
    with pytest.raises(dataclasses.FrozenInstanceError):
        query.text = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        _content().text = "x"  # type: ignore[misc]


def test_search_query_defaults_leave_filters_unspecified() -> None:
    query = SearchQuery(text="関ヶ原", kind="youtube", max_results=10)
    assert query.region_code is None and query.language is None
    assert query.published_after is None and query.published_before is None


def test_search_hit_carries_observations_with_their_observation_time() -> None:
    stat = StatObservation(metric="view_count", value=1200, unit="count", observed_at=NOW)
    hit = SearchHit(
        url="https://www.youtube.com/watch?v=abc",
        title="t",
        snippet="s",
        published_at=None,
        provider="youtube",
        provider_ref="abc",
        channel_id="c",
        channel_title="ch",
        stats=(stat,),
        channel_subscriber_count=None,
        duration_seconds=61,
        extras={},
    )
    assert hit.stats[0].observed_at == NOW
    assert hit.channel_subscriber_count is None


def test_search_results_defaults() -> None:
    query = SearchQuery(text="q", kind="web", max_results=3)
    results = SearchResults(
        query=query, hits=(), searched_at=NOW, provider="fake", cost_units=0, truncated=False
    )
    assert results.warnings == ()


def test_ports_are_structural_protocols() -> None:
    class S:
        name = "s"

        async def search(self, query: SearchQuery) -> SearchResults:
            raise NotImplementedError

    class F:
        async def fetch(self, url: str) -> FetchedContent:
            raise NotImplementedError

    provider: SearchProvider = S()
    fetcher: ContentFetcher = F()
    assert provider.name == "s" and fetcher is not None
