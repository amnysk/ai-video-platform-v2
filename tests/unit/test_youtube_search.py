"""YouTubeSearchProvider（ADR-0036 §3）。実ネットワークに出ない（MockTransport）。

守るもの: 呼び出し（search.list → videos.list → channels.list）の順序と quota 単位の計上、
統計に観測時刻が付くこと、``regionCode`` / ``relevanceLanguage`` を**指定した値**としてだけ残し
人気・言語を断定しないこと、``videoDuration`` で Shorts を判定しないこと、エラーの分類。
エンドポイントの文字列はこのファイルに書かない（INV-18。``API_BASE_URL`` を import する）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from domain.research.ports import SearchProvider, SearchQuery
from infrastructure.research.quota_costs import (
    YOUTUBE_CHANNELS_LIST_UNITS,
    YOUTUBE_SEARCH_LIST_UNITS,
    YOUTUBE_VIDEOS_LIST_UNITS,
)
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeRejectedError,
    YouTubeTransientError,
)
from infrastructure.youtube.search import YouTubeSearchProvider, parse_iso8601_duration
from infrastructure.youtube.uploader import API_BASE_URL

NOW = datetime(2026, 9, 21, 3, 0, tzinfo=UTC)
Handler = Callable[[httpx.Request], httpx.Response]

SEARCH_ITEMS = {
    "items": [
        {
            "id": {"kind": "youtube#video", "videoId": "vid1"},
            "snippet": {
                "title": "関ヶ原 &amp; 天下分け目 &#39;解説&#39;",
                "description": "説明1",
                "channelId": "chA",
                "channelTitle": "歴史A",
                "publishedAt": "2026-08-10T09:00:00Z",
            },
        },
        {
            "id": {"kind": "youtube#video", "videoId": "vid2"},
            "snippet": {
                "title": "30秒解説",
                "description": "説明2",
                "channelId": "chB",
                "channelTitle": "歴史B",
                "publishedAt": "2026-08-25T09:00:00Z",
            },
        },
    ]
}
VIDEO_ITEMS = {
    "items": [
        {
            "id": "vid1",
            "contentDetails": {"duration": "PT10M12S"},
            "statistics": {"viewCount": "152000", "likeCount": "4300", "commentCount": "210"},
        },
        {
            "id": "vid2",
            "contentDetails": {"duration": "PT30S"},
            "statistics": {"viewCount": "980000"},  # likeCount / commentCount は非公開
        },
    ]
}
CHANNEL_ITEMS = {
    "items": [
        {"id": "chA", "statistics": {"subscriberCount": "88000", "hiddenSubscriberCount": False}},
        {"id": "chB", "statistics": {"hiddenSubscriberCount": True}},
    ]
}


class Token:
    def __init__(self) -> None:
        self.issued = 0
        self.invalidated = 0

    async def access_token(self) -> str:
        self.issued += 1
        return f"token-{self.issued}"

    def invalidate(self) -> None:
        self.invalidated += 1


class Api:
    """path ごとに応答を返す。``responses[name]`` は dict（200）か httpx.Response / 例外。"""

    def __init__(self, **responses: object) -> None:
        self.responses = {"search": SEARCH_ITEMS, "videos": VIDEO_ITEMS, "channels": CHANNEL_ITEMS}
        self.responses.update(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        response = self.responses[name]
        if isinstance(response, Exception):
            raise response
        if isinstance(response, list):  # 呼び出しごとに順に返す
            response = response.pop(0)
        if isinstance(response, httpx.Response):
            return response
        return httpx.Response(200, json=response)

    def names(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


def _provider(api: Api, token: Token | None = None) -> tuple[YouTubeSearchProvider, Token]:
    token = token or Token()
    client = httpx.AsyncClient(transport=httpx.MockTransport(api))
    return YouTubeSearchProvider(token, client=client, clock=lambda: NOW), token


def _q(text: str = "関ヶ原", n: int = 10, **kw: object) -> SearchQuery:
    return SearchQuery(text=text, kind="youtube", max_results=n, **kw)  # type: ignore[arg-type]


def _quota_error() -> httpx.Response:
    return httpx.Response(
        403, json={"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}}
    )


def test_it_is_a_search_provider() -> None:
    provider: SearchProvider = YouTubeSearchProvider(
        Token(), client=httpx.AsyncClient(transport=httpx.MockTransport(Api()))
    )
    assert provider.name == "youtube"


async def test_the_full_flow_builds_hits_with_observation_times_and_counts_quota() -> None:
    api = Api()
    provider, _ = _provider(api)
    results = await provider.search(_q())
    assert api.names() == ["search", "videos", "channels"]
    assert all(str(r.url).startswith(API_BASE_URL) for r in api.requests)
    assert (
        results.cost_units
        == YOUTUBE_SEARCH_LIST_UNITS + YOUTUBE_VIDEOS_LIST_UNITS + YOUTUBE_CHANNELS_LIST_UNITS
        == 102
    )
    assert results.provider == "youtube" and results.searched_at == NOW
    assert results.truncated is False and results.warnings == ()

    first, second = results.hits
    assert first.url == "https://www.youtube.com/watch?v=vid1"
    assert first.provider_ref == "vid1" and first.provider == "youtube"
    assert first.title == "関ヶ原 & 天下分け目 '解説'"  # HTML エンティティを戻す
    assert first.snippet == "説明1"
    assert first.published_at == datetime(2026, 8, 10, 9, tzinfo=UTC)
    assert first.channel_id == "chA" and first.channel_title == "歴史A"
    assert first.duration_seconds == 612 and first.channel_subscriber_count == 88_000
    assert {(s.metric, s.value, s.unit) for s in first.stats} == {
        ("view_count", 152_000, "count"),
        ("like_count", 4_300, "count"),
        ("comment_count", 210, "count"),
    }
    assert all(s.observed_at == NOW for s in first.stats)
    # 非公開・不明な値は 0 にせず欠けたままにする
    assert {s.metric for s in second.stats} == {"view_count"}
    assert second.channel_subscriber_count is None
    assert second.duration_seconds == 30


async def test_request_parameters_and_bearer_token() -> None:
    api = Api()
    provider, _ = _provider(api)
    await provider.search(_q("関ヶ原 1600", n=7))
    search, videos, channels = api.requests
    params = dict(search.url.params)
    assert params["q"] == "関ヶ原 1600" and params["type"] == "video"
    assert params["part"] == "snippet" and params["maxResults"] == "7"
    assert "videoDuration" not in params  # 4 分未満の条件であって Shorts の判定ではない
    assert "regionCode" not in params and "relevanceLanguage" not in params
    assert dict(videos.url.params)["id"] == "vid1,vid2"
    assert set(dict(videos.url.params)["part"].split(",")) == {
        "snippet",
        "contentDetails",
        "statistics",
    }
    assert dict(channels.url.params)["id"] == "chA,chB"
    assert dict(channels.url.params)["part"] == "statistics"
    for request in api.requests:
        assert request.headers["authorization"].startswith("Bearer token-")
        assert request.method == "GET"


async def test_region_and_language_are_filters_recorded_as_requested_values_only() -> None:
    """``regionCode`` は「その地域で視聴可能な動画」への絞り込み、``relevanceLanguage`` は
    言語への関連度の重みづけ。どちらも人気・視聴者層・動画の言語の断定ではない。"""
    api = Api()
    provider, _ = _provider(api)
    results = await provider.search(_q(region_code="US", language="en"))
    params = dict(api.requests[0].url.params)
    assert params["regionCode"] == "US" and params["relevanceLanguage"] == "en"
    for hit in results.hits:
        assert hit.extras == {
            "requested_region_code": "US",
            "requested_relevance_language": "en",
        }


async def test_published_range_is_sent_as_rfc3339_utc() -> None:
    api = Api()
    provider, _ = _provider(api)
    jst = datetime.fromisoformat("2026-08-01T09:00:00+09:00")
    await provider.search(
        _q(published_after=jst, published_before=datetime(2026, 9, 1, tzinfo=UTC))
    )
    params = dict(api.requests[0].url.params)
    assert params["publishedAfter"] == "2026-08-01T00:00:00Z"
    assert params["publishedBefore"] == "2026-09-01T00:00:00Z"


async def test_a_naive_datetime_is_rejected_before_any_call() -> None:
    api = Api()
    provider, _ = _provider(api)
    with pytest.raises(ValueError):
        await provider.search(_q(published_after=datetime(2026, 8, 1)))
    assert api.requests == []


@pytest.mark.parametrize(
    "query",
    [
        SearchQuery(text="x", kind="web", max_results=5),
        SearchQuery(text="  ", kind="youtube", max_results=5),
        SearchQuery(text="x", kind="youtube", max_results=0),
    ],
)
async def test_invalid_queries_are_rejected_before_any_call(query: SearchQuery) -> None:
    api = Api()
    provider, _ = _provider(api)
    with pytest.raises(ValueError):
        await provider.search(query)
    assert api.requests == []


async def test_max_results_is_capped_at_the_api_page_size_and_reported_as_truncated() -> None:
    api = Api()
    provider, _ = _provider(api)
    results = await provider.search(_q(n=200))
    assert dict(api.requests[0].url.params)["maxResults"] == "50"
    assert results.truncated is True


async def test_a_next_page_token_means_truncated() -> None:
    api = Api(search={**SEARCH_ITEMS, "nextPageToken": "CAoQAA"})
    provider, _ = _provider(api)
    assert (await provider.search(_q(n=2))).truncated is True


async def test_no_results_costs_only_the_search() -> None:
    api = Api(search={"items": []})
    provider, _ = _provider(api)
    results = await provider.search(_q())
    assert api.names() == ["search"]
    assert results.hits == () and results.cost_units == YOUTUBE_SEARCH_LIST_UNITS


async def test_a_video_missing_from_videos_list_keeps_its_search_data_without_stats() -> None:
    api = Api(videos={"items": [VIDEO_ITEMS["items"][0]]})
    provider, _ = _provider(api)
    results = await provider.search(_q())
    gone = results.hits[1]
    assert gone.stats == () and gone.duration_seconds is None
    assert gone.title == "30秒解説" and gone.channel_id == "chB"
    assert any("vid2" in w or "missing" in w for w in results.warnings)


async def test_duplicate_channels_are_requested_once() -> None:
    items = {"items": [SEARCH_ITEMS["items"][0], SEARCH_ITEMS["items"][0]]}
    api = Api(search=items)
    provider, _ = _provider(api)
    await provider.search(_q())
    assert dict(api.requests[1].url.params)["id"] == "vid1"
    assert dict(api.requests[2].url.params)["id"] == "chA"


async def test_channel_lookup_failure_degrades_to_a_warning_and_still_counts_its_cost() -> None:
    api = Api(channels=_quota_error())
    provider, _ = _provider(api)
    results = await provider.search(_q())
    assert all(h.channel_subscriber_count is None for h in results.hits)
    assert results.hits[0].stats  # 検索と統計は使える
    assert any("channels_list_failed" in w for w in results.warnings)
    assert results.cost_units == 102  # 送った呼び出しは課金されたものとして数える


async def test_video_details_failure_degrades_to_a_warning() -> None:
    api = Api(videos=httpx.Response(500))
    provider, _ = _provider(api)
    results = await provider.search(_q())
    assert results.hits and all(h.stats == () for h in results.hits)
    assert any("videos_list_failed" in w for w in results.warnings)
    assert results.cost_units == 102


# --- エラーの分類 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (_quota_error(), YouTubeQuotaError),
        (httpx.Response(429), YouTubeRateLimitError),
        (httpx.Response(500), YouTubeTransientError),
        (httpx.Response(503), YouTubeTransientError),
        (
            httpx.Response(400, json={"error": {"errors": [{"reason": "invalidVideoMetadata"}]}}),
            YouTubeRejectedError,
        ),
        (
            httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}}),
            YouTubeAuthError,
        ),
    ],
)
async def test_search_errors_are_classified(
    response: httpx.Response, error: type[Exception]
) -> None:
    provider, _ = _provider(Api(search=response))
    with pytest.raises(error):
        await provider.search(_q())


async def test_a_transport_failure_is_transient_and_hides_the_token() -> None:
    provider, _ = _provider(Api(search=httpx.ConnectError("boom token-1")))
    with pytest.raises(YouTubeTransientError) as caught:
        await provider.search(_q())
    assert "token-1" not in str(caught.value)


async def test_a_401_refreshes_the_token_once_then_succeeds() -> None:
    api = Api(search=[httpx.Response(401), httpx.Response(200, json=SEARCH_ITEMS)])
    provider, token = _provider(api)
    results = await provider.search(_q())
    assert token.invalidated == 1 and results.hits
    assert api.requests[0].headers["authorization"] == "Bearer token-1"
    assert api.requests[1].headers["authorization"] == "Bearer token-2"


async def test_a_second_401_is_an_auth_error() -> None:
    provider, token = _provider(Api(search=[httpx.Response(401), httpx.Response(401)]))
    with pytest.raises(YouTubeAuthError):
        await provider.search(_q())
    assert token.invalidated == 2


async def test_a_malformed_body_is_transient() -> None:
    provider, _ = _provider(Api(search=httpx.Response(200, content=b"not json")))
    with pytest.raises(YouTubeTransientError):
        await provider.search(_q())


async def test_errors_never_carry_response_bodies_or_tokens() -> None:
    body = {"error": {"message": "token-1 leaked", "errors": [{"reason": "quotaExceeded"}]}}
    provider, _ = _provider(Api(search=httpx.Response(403, json=body)))
    with pytest.raises(YouTubeQuotaError) as caught:
        await provider.search(_q())
    assert "token-1" not in str(caught.value)
    assert "token-1" not in json.dumps(str(caught.value))


# --- 期間の解析 -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("PT10M12S", 612),
        ("PT30S", 30),
        ("PT1H2M3S", 3723),
        ("PT1H", 3600),
        ("PT5M", 300),
        ("P1DT1H", 90_000),
        ("P0D", 0),
        ("", None),
        ("10:12", None),
        ("PT", None),
        ("garbage", None),
        (None, None),
    ],
)
def test_iso8601_duration(value: str | None, seconds: int | None) -> None:
    assert parse_iso8601_duration(value) == seconds
