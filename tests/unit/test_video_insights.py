"""動画別 insight adapter（ADR-0047 §5）。httpx.MockTransport のみ（実ネットワーク無し / INV-18）。

各テストの理由は docs/testing/agent-tools-rationale.md。
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from contracts.agent_tools import ToolStatus
from infrastructure.analytics.video_insights import YouTubeVideoInsightProvider
from infrastructure.analytics.youtube_analytics import ANALYTICS_SCOPE, REPORTS_ENDPOINT
from infrastructure.youtube.data_reader import (
    CHANNELS_LIST_URL,
    DATA_API_READ_URLS,
    DATA_READONLY_SCOPE,
    PLAYLIST_ITEMS_URL,
    VIDEOS_LIST_URL,
)
from infrastructure.youtube.oauth import (
    TOKEN_ENDPOINT,
    TOKENINFO_ENDPOINT,
    RefreshTokenCredentials,
)

TODAY = date(2026, 9, 20)
CONFIRMED = date(2026, 9, 17)  # TODAY - 3 日
VID = "abcdefghijk"
ALL_SCOPES = f"{ANALYTICS_SCOPE} {DATA_READONLY_SCOPE}"
ALLOWED_GET = {REPORTS_ENDPOINT, *DATA_API_READ_URLS}


def report(headers: list[str], rows: list[list[Any]]) -> httpx.Response:
    return httpx.Response(200, json={"columnHeaders": [{"name": h} for h in headers], "rows": rows})


def video_item(vid: str, published: str, duration: str = "PT1M", live: str = "none") -> dict:
    return {
        "id": vid,
        "snippet": {"publishedAt": published, "liveBroadcastContent": live},
        "contentDetails": {"duration": duration},
    }


class Net:
    """全リクエストを記録し、URL ごとに handler を振り分ける。"""

    def __init__(
        self,
        analytics: Callable[[httpx.Request], httpx.Response],
        data: Callable[[httpx.Request], httpx.Response] | None = None,
        *,
        scopes: str | None = ALL_SCOPES,
    ) -> None:
        self.analytics = analytics
        self.data = data or (lambda _r: httpx.Response(404))
        self.scopes = scopes
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.tokeninfo_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        if url == TOKEN_ENDPOINT:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "SECRET-TOK", "expires_in": 3600})
        if url == TOKENINFO_ENDPOINT:
            self.tokeninfo_calls += 1
            if self.scopes is None:
                return httpx.Response(400, json={"error": "invalid_token"})
            return httpx.Response(200, json={"scope": self.scopes})
        self.requests.append(req)
        return self.analytics(req) if url == REPORTS_ENDPOINT else self.data(req)

    def analytics_params(self) -> list[dict[str, str]]:
        return [
            dict(r.url.params) for r in self.requests if str(r.url).startswith(REPORTS_ENDPOINT)
        ]


def make(net: Net) -> YouTubeVideoInsightProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(net))
    creds = RefreshTokenCredentials(
        "client-id", SecretStr("SECRET-CLIENT"), SecretStr("SECRET-REFRESH"), client=client
    )
    return YouTubeVideoInsightProvider(
        creds,
        client=client,
        today=lambda: TODAY,
        now=lambda: datetime(2026, 9, 20, 1, 2, 3, tzinfo=UTC),
    )


def videos_handler(*items: dict) -> Callable[[httpx.Request], httpx.Response]:
    def handle(req: httpx.Request) -> httpx.Response:
        assert str(req.url).startswith(VIDEOS_LIST_URL)
        ids = req.url.params["id"].split(",")
        return httpx.Response(200, json={"items": [i for i in items if i["id"] in ids]})

    return handle


# ------------------------------------------------------------------ get_video_analytics

VIDEO_METRICS = (
    "views,engagedViews,estimatedMinutesWatched,averageViewDuration,likes,comments,"
    "subscribersGained"
)


@pytest.mark.asyncio
async def test_video_analytics_sends_exact_query_and_maps_values() -> None:
    net = Net(
        lambda _r: report(VIDEO_METRICS.split(","), [[100, 80, 50.5, 21.0, 7, 3, 2]]),
        videos_handler(video_item(VID, "2026-09-01T10:00:00Z")),
    )
    res = await make(net).video_analytics(VID, None, None, None)
    assert res.status is ToolStatus.OK
    assert net.analytics_params() == [
        {
            "ids": "channel==MINE",
            "startDate": "2026-09-01",
            "endDate": "2026-09-17",
            "metrics": VIDEO_METRICS,
            "filters": f"video=={VID}",
        }
    ]
    video_req = next(r for r in net.requests if str(r.url).startswith(VIDEOS_LIST_URL))
    assert dict(video_req.url.params) == {
        "part": "snippet,contentDetails",
        "id": VID,
    }
    d = res.data
    assert d is not None
    assert (d.views, d.engaged_views, d.comments) == (100.0, 80.0, 3.0)  # type: ignore[attr-defined]
    assert d.average_view_duration_seconds == 21.0  # type: ignore[attr-defined]
    assert d.published_at == "2026-09-01T10:00:00+00:00"  # type: ignore[attr-defined]
    assert res.quota_units == "unknown"
    assert res.retrieved_at == "2026-09-20T01:02:03+00:00"
    assert len(res.evidence_refs) == 1 and res.evidence_refs[0].startswith("analytics_query:")


@pytest.mark.asyncio
async def test_video_analytics_explicit_range_skips_metadata_and_honours_metric_subset() -> None:
    net = Net(lambda _r: report(["views", "likes"], [[5, 1]]))
    res = await make(net).video_analytics(
        VID, date(2026, 9, 1), date(2026, 9, 10), ("views", "likes")
    )
    assert res.status is ToolStatus.OK
    assert net.analytics_params()[0]["metrics"] == "views,likes"
    assert all(str(r.url).startswith(REPORTS_ENDPOINT) for r in net.requests)
    assert res.data.comments is None  # type: ignore[union-attr]  # 取れていない値は 0 ではない


@pytest.mark.asyncio
async def test_evidence_ref_hash_is_stable_and_excludes_credentials() -> None:
    def run() -> Any:
        net = Net(lambda _r: report(["views"], [[1]]))
        return make(net).video_analytics(VID, date(2026, 9, 1), date(2026, 9, 2), ("views",))

    a, b = await run(), await run()
    assert a.evidence_refs == b.evidence_refs
    assert "SECRET" not in repr(a)


@pytest.mark.asyncio
@pytest.mark.parametrize("metric", ["impressions", "impressionsClickThroughRate"])
async def test_impressions_and_ctr_are_not_available_and_never_fabricated(metric: str) -> None:
    net = Net(lambda _r: report(["views"], [[1]]))
    res = await make(net).video_analytics(
        VID,
        date(2026, 9, 1),
        date(2026, 9, 2),
        ("views", metric),  # type: ignore[arg-type]
    )
    assert res.status is ToolStatus.NOT_AVAILABLE_VIA_API
    assert res.data is None
    assert net.requests == []  # API を呼ばない
    assert f"not_available_via_api:{metric}" in res.limitations


@pytest.mark.asyncio
async def test_empty_rows_is_no_data_not_zero() -> None:
    net = Net(lambda _r: report(VIDEO_METRICS.split(","), []))
    res = await make(net).video_analytics(VID, date(2026, 9, 1), date(2026, 9, 2), None)
    assert res.status is ToolStatus.NO_DATA and res.data is None


@pytest.mark.asyncio
async def test_range_inside_processing_window_is_reporting_delay_without_calling() -> None:
    net = Net(lambda _r: report([], []))
    res = await make(net).video_analytics(VID, date(2026, 9, 18), date(2026, 9, 19), None)
    assert res.status is ToolStatus.REPORTING_DELAY
    assert net.requests == []
    assert "latest_confirmed_date:2026-09-17" in res.limitations


@pytest.mark.asyncio
async def test_video_published_inside_processing_window_is_reporting_delay() -> None:
    net = Net(
        lambda _r: report([], []),
        videos_handler(video_item(VID, "2026-09-19T10:00:00Z")),
    )
    res = await make(net).video_analytics(VID, None, None, None)
    assert res.status is ToolStatus.REPORTING_DELAY


@pytest.mark.asyncio
async def test_unknown_video_is_no_data() -> None:
    net = Net(lambda _r: report([], []), videos_handler())
    res = await make(net).video_analytics(VID, None, None, None)
    assert res.status is ToolStatus.NO_DATA and res.error_code == "video_not_found"


@pytest.mark.asyncio
async def test_old_windows_carry_the_shorts_views_definition_limitation() -> None:
    net = Net(lambda _r: report(["views"], [[1]]))
    old = await make(net).video_analytics(VID, date(2025, 4, 1), date(2025, 6, 1), ("views",))
    new = await make(net).video_analytics(VID, date(2025, 6, 1), date(2025, 7, 1), ("views",))
    assert "shorts_views_definition_changed_2025_03_31" in old.limitations
    assert "shorts_views_definition_changed_2025_03_31" not in new.limitations


# ------------------------------------------------------------------ error classification


def _err(status: int, reason: str | None = None) -> Callable[[httpx.Request], httpx.Response]:
    body: dict[str, Any] = {"error": {"message": "SECRET-TOK in body"}}
    if reason:
        body["error"]["errors"] = [{"reason": reason}]
    return lambda _r: httpx.Response(status, json=body)


async def _call(net: Net) -> Any:
    return await make(net).video_analytics(VID, date(2026, 9, 1), date(2026, 9, 2), ("views",))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "reason", "expected", "code"),
    [
        (403, "quotaExceeded", ToolStatus.QUOTA_EXCEEDED, "quotaExceeded"),
        (403, "dailyLimitExceeded", ToolStatus.QUOTA_EXCEEDED, "dailyLimitExceeded"),
        (403, "rateLimitExceeded", ToolStatus.RATE_LIMITED, "rateLimitExceeded"),
        (429, None, ToolStatus.RATE_LIMITED, "http_429"),
        (400, None, ToolStatus.FAILED, "invalid_query"),
        (500, None, ToolStatus.FAILED, "http_500"),
    ],
)
async def test_http_errors_map_to_statuses(
    status: int, reason: str | None, expected: ToolStatus, code: str
) -> None:
    net = Net(_err(status, reason))
    res = await _call(net)
    assert (res.status, res.error_code) == (expected, code)
    assert res.data is None
    assert net.tokeninfo_calls == 0  # quota / rate の 403 では scope を疑わない
    assert "SECRET" not in repr(res)


@pytest.mark.asyncio
async def test_403_with_missing_analytics_scope_is_permission_denied_scope_missing() -> None:
    net = Net(_err(403, "forbidden"), scopes=DATA_READONLY_SCOPE)
    res = await _call(net)
    assert res.status is ToolStatus.PERMISSION_DENIED and res.error_code == "scope_missing"
    assert net.tokeninfo_calls == 1


@pytest.mark.asyncio
async def test_403_with_scope_present_is_permission_denied_with_the_reason() -> None:
    res = await _call(Net(_err(403, "forbidden")))
    assert res.status is ToolStatus.PERMISSION_DENIED and res.error_code == "forbidden:forbidden"


@pytest.mark.asyncio
async def test_403_when_tokeninfo_is_unavailable_is_still_permission_denied() -> None:
    res = await _call(Net(_err(403, "forbidden"), scopes=None))
    assert res.status is ToolStatus.PERMISSION_DENIED
    assert res.error_code == "forbidden_scope_unverified"


@pytest.mark.asyncio
async def test_401_refreshes_the_token_once_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(_r: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401) if calls["n"] == 1 else report(["views"], [[3]])

    net = Net(handler)
    res = await _call(net)
    assert res.status is ToolStatus.OK
    assert net.token_calls == 2  # 最初の取得 + 401 後の更新（1 回だけ）


@pytest.mark.asyncio
async def test_persistent_401_gives_up_after_one_refresh() -> None:
    net = Net(lambda _r: httpx.Response(401))
    res = await _call(net)
    assert res.status is ToolStatus.PERMISSION_DENIED and res.error_code == "unauthorized"
    assert len(net.analytics_params()) == 2 and net.token_calls == 2


@pytest.mark.asyncio
async def test_timeouts_and_transport_errors_are_states_not_exceptions() -> None:
    def timeout(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow https://x/?access_token=SECRET-TOK")

    def broken(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom SECRET-TOK")

    t, c = await _call(Net(timeout)), await _call(Net(broken))
    assert t.status is ToolStatus.TIMEOUT and c.status is ToolStatus.FAILED
    assert "SECRET" not in repr(t) + repr(c)


@pytest.mark.asyncio
async def test_malformed_body_is_failed_malformed_response() -> None:
    res = await _call(Net(lambda _r: httpx.Response(200, json={"rows": [[1]]})))
    assert res.status is ToolStatus.FAILED and res.error_code == "malformed_response"


# ------------------------------------------------------------------ get_audience_retention


@pytest.mark.asyncio
async def test_retention_query_points_and_derived_drops() -> None:
    rows = [[0.01, 1.0, 1.1], [0.02, 0.9, 1.0], [0.03, 0.4, 0.8], [0.04, 0.45, 0.8]]
    net = Net(
        lambda _r: report(
            ["elapsedVideoTimeRatio", "audienceWatchRatio", "relativeRetentionPerformance"], rows
        )
    )
    res = await make(net).audience_retention(VID, date(2026, 9, 1), date(2026, 9, 10))
    assert net.analytics_params() == [
        {
            "ids": "channel==MINE",
            "startDate": "2026-09-01",
            "endDate": "2026-09-10",
            "dimensions": "elapsedVideoTimeRatio",
            "metrics": "audienceWatchRatio,relativeRetentionPerformance",
            "filters": f"video=={VID}",
        }
    ]
    d = res.data
    assert res.status is ToolStatus.OK and d is not None
    assert [p.position_ratio for p in d.points] == [0.01, 0.02, 0.03, 0.04]  # type: ignore[attr-defined]
    assert (d.largest_drops[0].from_ratio, d.largest_drops[0].to_ratio) == (0.02, 0.03)  # type: ignore[attr-defined]
    assert d.derived_fields == ("largest_drops",)  # type: ignore[attr-defined]
    assert "largest_drops_is_derived" in res.limitations


@pytest.mark.asyncio
async def test_retention_empty_is_no_data() -> None:
    net = Net(lambda _r: report(["elapsedVideoTimeRatio"], []))
    res = await make(net).audience_retention(VID, date(2026, 9, 1), date(2026, 9, 10))
    assert res.status is ToolStatus.NO_DATA


# ------------------------------------------------------------------ get_audience_demographics


def _demo(age_rows: list[list[Any]], country_rows: list[list[Any]]) -> Net:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.params["dimensions"] == "ageGroup,gender":
            return report(["ageGroup", "gender", "viewerPercentage"], age_rows)
        return report(["country", "views"], country_rows)

    return Net(handle)


@pytest.mark.asyncio
async def test_demographics_queries_and_values() -> None:
    net = _demo([["age18-24", "male", 12.5], ["age25-34", "female", 7.5]], [["JP", 30], ["US", 10]])
    res = await make(net).audience_demographics(VID, date(2026, 8, 1), date(2026, 8, 28))
    base = {"ids": "channel==MINE", "startDate": "2026-08-01", "endDate": "2026-08-28"}
    assert net.analytics_params() == [
        {
            **base,
            "dimensions": "ageGroup,gender",
            "metrics": "viewerPercentage",
            "filters": f"video=={VID}",
        },
        {**base, "dimensions": "country", "metrics": "views", "filters": f"video=={VID}"},
    ]
    d = res.data
    assert res.status is ToolStatus.OK and d is not None
    assert d.age_gender[0].viewer_percentage == 12.5  # type: ignore[index]
    assert [(c.country, c.share_of_views) for c in d.countries] == [  # type: ignore[union-attr]
        ("JP", 0.75),
        ("US", 0.25),
    ]
    assert "viewer_percentage_not_summable_across_dimensions" in res.limitations
    assert not hasattr(d, "total_viewer_percentage")


@pytest.mark.asyncio
async def test_demographics_channel_wide_has_no_video_filter_and_default_28_days() -> None:
    net = _demo([["age18-24", "male", 100.0]], [["JP", 1]])
    await make(net).audience_demographics(None, None, None)
    assert all("filters" not in p for p in net.analytics_params())
    assert net.analytics_params()[0]["startDate"] == "2026-08-21"
    assert net.analytics_params()[0]["endDate"] == "2026-09-17"


@pytest.mark.asyncio
async def test_demographics_missing_rows_are_withheld_not_zero() -> None:
    res = await make(_demo([], [])).audience_demographics(VID, date(2026, 8, 1), date(2026, 8, 28))
    assert res.status is ToolStatus.WITHHELD_BY_THRESHOLD and res.data is None


@pytest.mark.asyncio
async def test_demographics_partial_withholding_keeps_other_section_and_says_so() -> None:
    net = _demo([], [["JP", 1]])
    res = await make(net).audience_demographics(VID, date(2026, 8, 1), date(2026, 8, 28))
    assert res.status is ToolStatus.OK
    assert res.data.age_gender is None  # type: ignore[union-attr]
    assert "age_gender_withheld_by_threshold" in res.limitations


# ------------------------------------------------------------------ get_channel_performance

DAILY = ["day", "creatorContentType", *["views", "engagedViews", "estimatedMinutesWatched"]]
DAILY += ["subscribersGained", "subscribersLost"]


def _channel_net(
    daily_rows: list[list[Any]], *, pages: list[dict] | None = None, end_day: str = "2026-09-17"
) -> Net:
    totals = ["views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration"]
    totals += ["subscribersGained", "subscribersLost"]

    def analytics(req: httpx.Request) -> httpx.Response:
        if "dimensions" in req.url.params:
            return report(DAILY, daily_rows)
        return report(totals, [[300, 200, 90, 22.0, 9, 2]])

    pages_ = list(pages or [{"items": [], "nextPageToken": None}])

    def data(req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        if url == CHANNELS_LIST_URL:
            return httpx.Response(
                200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
            )
        assert url == PLAYLIST_ITEMS_URL
        return httpx.Response(200, json=pages_.pop(0))

    return Net(analytics, data)


def _page(*dates: str, token: str | None = None) -> dict:
    return {
        "items": [{"contentDetails": {"videoPublishedAt": f"{d}T00:00:00Z"}} for d in dates],
        "nextPageToken": token,
    }


@pytest.mark.asyncio
async def test_channel_performance_queries_totals_daily_trend_and_split() -> None:
    rows = [
        ["2026-09-16", "SHORTS", 10, 8, 3, 2, 1],
        ["2026-09-16", "VIDEO_ON_DEMAND", 5, 4, 6, 1, 0],
        ["2026-09-17", "SHORTS", 20, 15, 4, 3, 1],
    ]
    net = _channel_net(rows, pages=[_page("2026-09-16", "2026-09-01", "2026-08-01")])
    res = await make(net).channel_performance(date(2026, 9, 16), date(2026, 9, 17))
    base = {"ids": "channel==MINE", "startDate": "2026-09-16", "endDate": "2026-09-17"}
    assert net.analytics_params() == [
        {
            **base,
            "metrics": "views,engagedViews,estimatedMinutesWatched,averageViewDuration,"
            "subscribersGained,subscribersLost",
        },
        {
            **base,
            "dimensions": "day,creatorContentType",
            "metrics": "views,engagedViews,estimatedMinutesWatched,subscribersGained,"
            "subscribersLost",
        },
    ]
    d = res.data
    assert res.status is ToolStatus.OK and d is not None
    assert d.views == 300.0 and d.subscribers_lost == 2.0  # type: ignore[attr-defined]
    shorts = next(f for f in d.by_format if f.content_type == "SHORTS")  # type: ignore[attr-defined]
    assert (shorts.views, shorts.subscribers_gained) == (30.0, 5.0)
    assert [(x.date.isoformat(), x.subscribers_gained) for x in d.daily_subscribers] == [  # type: ignore[attr-defined]
        ("2026-09-16", 3.0),
        ("2026-09-17", 3.0),
    ]
    assert d.published_count == 1  # type: ignore[attr-defined]  # 範囲内は 09-16 のみ
    assert "published_count_not_split_by_format" in res.limitations


@pytest.mark.asyncio
async def test_published_count_uses_documented_cheap_endpoints_not_search() -> None:
    net = _channel_net([["2026-09-17", "SHORTS", 1, 1, 1, 0, 0]], pages=[_page("2026-09-17")])
    await make(net).channel_performance(date(2026, 9, 17), date(2026, 9, 17))
    data_reqs = [r for r in net.requests if not str(r.url).startswith(REPORTS_ENDPOINT)]
    assert [str(r.url).split("?")[0] for r in data_reqs] == [CHANNELS_LIST_URL, PLAYLIST_ITEMS_URL]
    assert dict(data_reqs[0].url.params) == {"part": "contentDetails", "mine": "true"}
    assert dict(data_reqs[1].url.params) == {
        "part": "contentDetails",
        "playlistId": "UUx",
        "maxResults": "50",
    }
    assert all("search" not in str(r.url) for r in net.requests)


@pytest.mark.asyncio
async def test_published_count_paginates_until_a_page_is_entirely_older() -> None:
    pages = [
        _page("2026-09-17", "2026-09-05", token="p2"),
        _page("2026-08-20", "2026-08-01", token="p3"),
        _page("2026-09-12"),  # 読まれない
    ]
    net = _channel_net([["2026-09-17", "SHORTS", 1, 1, 1, 0, 0]], pages=pages)
    res = await make(net).channel_performance(date(2026, 9, 1), date(2026, 9, 17))
    assert res.data.published_count == 2  # type: ignore[union-attr]
    assert len([r for r in net.requests if str(r.url).startswith(PLAYLIST_ITEMS_URL)]) == 2


@pytest.mark.asyncio
async def test_channel_daily_series_ending_before_requested_end_is_reporting_delay() -> None:
    net = _channel_net([["2026-09-15", "SHORTS", 1, 1, 1, 0, 0]])
    res = await make(net).channel_performance(date(2026, 9, 15), date(2026, 9, 17))
    assert res.status is ToolStatus.REPORTING_DELAY and res.data is None
    assert "last_day_returned:2026-09-15" in res.limitations


@pytest.mark.asyncio
async def test_channel_empty_is_no_data() -> None:
    def analytics(_r: httpx.Request) -> httpx.Response:
        return report(["views"], [])

    res = await make(Net(analytics)).channel_performance(date(2026, 9, 1), date(2026, 9, 2))
    assert res.status is ToolStatus.NO_DATA


# ------------------------------------------------------------------ compare_video_performance

CMP_HEADERS = [
    "day",
    "creatorContentType",
    *["views", "engagedViews", "estimatedMinutesWatched"],
]
CMP_HEADERS += ["averageViewDuration", "averageViewPercentage"]


def _cmp_net(metas: list[dict], per_video: dict[str, list[list[Any]]]) -> Net:
    def analytics(req: httpx.Request) -> httpx.Response:
        vid = req.url.params["filters"].removeprefix("video==")
        return report(CMP_HEADERS, per_video.get(vid, []))

    return Net(analytics, videos_handler(*metas))


def _days(kind: str, views: list[int], avg: float = 10.0, pct: float = 50.0) -> list[list[Any]]:
    return [
        [f"2026-09-{i + 1:02d}", kind, v, v // 2, v / 10, avg, pct] for i, v in enumerate(views)
    ]


def _ids(*names: str) -> tuple[str, ...]:
    return tuple(f"{n:<11}".replace(" ", "x") for n in names)


A, B, C, D = _ids("a", "b", "c", "d")


@pytest.mark.asyncio
async def test_compare_aligns_by_days_since_publish_and_groups_by_format() -> None:
    # A,B: 長尺（経過 12 日 / 10 日）、C,D: Shorts（2026 年なので views も比較可）
    metas = [
        video_item(A, "2026-09-06T00:00:00Z", "PT10M"),
        video_item(B, "2026-09-08T00:00:00Z", "PT5M"),
        video_item(C, "2026-09-08T00:00:00Z", "PT30S"),
        video_item(D, "2026-09-08T00:00:00Z", "PT45S"),
    ]
    per = {
        A: _days("VIDEO_ON_DEMAND", [10, 10, 10], avg=60.0, pct=40.0),
        B: _days("VIDEO_ON_DEMAND", [30, 30, 30], avg=100.0, pct=60.0),
        C: _days("SHORTS", [100, 100, 100]),
        D: _days("SHORTS", [50, 50, 50]),
    }
    net = _cmp_net(metas, per)
    res = await make(net).compare_videos((A, B, C, D), None)
    assert res.status is ToolStatus.OK
    out = res.data
    assert out is not None
    # 最小経過日数 = B/C/D の 10 日（09-08〜09-17）
    assert out.window_days == 10  # type: ignore[attr-defined]
    qs = {p["filters"]: p for p in net.analytics_params()}
    assert qs[f"video=={A}"]["startDate"] == "2026-09-06"
    assert qs[f"video=={A}"]["endDate"] == "2026-09-15"  # 公開日 + 9
    assert qs[f"video=={B}"]["startDate"] == "2026-09-08"
    assert qs[f"video=={B}"]["endDate"] == "2026-09-17"
    assert qs[f"video=={A}"]["dimensions"] == "day,creatorContentType"
    assert qs[f"video=={A}"]["metrics"] == (
        "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage"
    )
    groups = {g.content_type: g for g in out.groups}  # type: ignore[attr-defined]
    assert set(groups) == {"SHORTS", "VIDEO_ON_DEMAND"}
    for g in groups.values():  # Shorts と長尺は混ざらない
        kinds = {v.video_id for v in g.videos}
        assert kinds == ({A, B} if g.content_type == "VIDEO_ON_DEMAND" else {C, D})
    long = groups["VIDEO_ON_DEMAND"]
    assert long.ranking_within_group["engaged_views"] == (B, A)
    assert long.ranking_within_group["average_view_percentage"] == (B, A)
    assert next(v for v in long.videos if v.video_id == B).average_view_percentage == 60.0
    assert "compare_never_ranks_across_content_types" in res.limitations


@pytest.mark.asyncio
async def test_compare_weighted_average_is_views_weighted() -> None:
    metas = [video_item(A, "2026-09-01T00:00:00Z"), video_item(B, "2026-09-01T00:00:00Z")]
    rows = [
        ["2026-09-01", "VIDEO_ON_DEMAND", 10, 5, 1, 10.0, 20.0],
        ["2026-09-02", "VIDEO_ON_DEMAND", 30, 15, 3, 50.0, 60.0],
    ]
    net = _cmp_net(metas, {A: rows, B: rows})
    res = await make(net).compare_videos((A, B), 3)
    v = res.data.groups[0].videos[0]  # type: ignore[union-attr]
    assert v.average_view_duration_seconds == pytest.approx(40.0)
    assert v.average_view_percentage == pytest.approx(50.0)
    assert (v.views, v.engaged_views) == (40.0, 20.0)


@pytest.mark.asyncio
async def test_compare_shorts_crossing_2025_views_change_marks_views_insufficient() -> None:
    old = "2025-04-10T00:00:00Z"
    metas = [video_item(A, old, "PT30S"), video_item(B, old, "PT30S")]
    net = _cmp_net(metas, {A: _days("SHORTS", [5] * 3), B: _days("SHORTS", [6] * 3)})
    # TODAY を 2025-04-20 付近に置く（確定日 04-17、経過 8 日）
    p = make(net)
    p._today = lambda: date(2025, 4, 20)  # type: ignore[assignment]
    res = await p.compare_videos((A, B), None)
    g = res.data.groups[0]  # type: ignore[union-attr]
    assert g.views_status == "insufficient_data"
    assert all(v.views is None for v in g.videos)
    assert "views" not in g.ranking_within_group and "engaged_views" in g.ranking_within_group
    assert any(
        x.startswith("views_insufficient_data_shorts_definition_change") for x in res.limitations
    )


@pytest.mark.asyncio
async def test_compare_long_form_across_2025_cutover_keeps_views() -> None:
    old = "2025-04-10T00:00:00Z"
    metas = [video_item(A, old), video_item(B, old)]
    net = _cmp_net(
        metas, {A: _days("VIDEO_ON_DEMAND", [5] * 3), B: _days("VIDEO_ON_DEMAND", [6] * 3)}
    )
    p = make(net)
    p._today = lambda: date(2025, 4, 20)  # type: ignore[assignment]
    res = await p.compare_videos((A, B), None)
    assert res.data.groups[0].views_status == "ok"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_compare_group_with_single_video_is_insufficient_and_unranked() -> None:
    metas = [
        video_item(A, "2026-09-01T00:00:00Z"),
        video_item(B, "2026-09-01T00:00:00Z"),
        video_item(C, "2026-09-01T00:00:00Z"),
    ]
    per = {
        A: _days("VIDEO_ON_DEMAND", [5] * 3),
        B: _days("VIDEO_ON_DEMAND", [6] * 3),
        C: _days("SHORTS", [100] * 3),
    }
    res = await make(_cmp_net(metas, per)).compare_videos((A, B, C), None)
    groups = {g.content_type: g for g in res.data.groups}  # type: ignore[union-attr]
    assert groups["SHORTS"].status == "insufficient_data"
    assert groups["SHORTS"].ranking_within_group == {}
    assert groups["VIDEO_ON_DEMAND"].status == "ok"


@pytest.mark.asyncio
async def test_compare_all_groups_insufficient_is_insufficient_data() -> None:
    metas = [video_item(A, "2026-09-01T00:00:00Z"), video_item(B, "2026-09-01T00:00:00Z")]
    per = {A: _days("VIDEO_ON_DEMAND", [5] * 3), B: _days("SHORTS", [6] * 3)}
    res = await make(_cmp_net(metas, per)).compare_videos((A, B), None)
    assert res.status is ToolStatus.INSUFFICIENT_DATA and res.data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("published_b", "code"),
    [
        ("2026-09-16T00:00:00Z", "window_too_short"),  # 経過 2 日
        ("2026-09-12T00:00:00Z", "ages_differ_too_much"),  # 6 日 vs 数百日
    ],
)
async def test_compare_window_too_short_or_ages_differ_too_much(
    published_b: str, code: str
) -> None:
    old = "2025-12-01T00:00:00Z" if code == "ages_differ_too_much" else "2026-09-01T00:00:00Z"
    metas = [video_item(A, old), video_item(B, published_b)]
    net = _cmp_net(metas, {})
    res = await make(net).compare_videos((A, B), None)
    assert res.status is ToolStatus.INSUFFICIENT_DATA and res.error_code == code
    assert net.analytics_params() == []


@pytest.mark.asyncio
async def test_compare_explicit_days_drops_younger_videos_and_needs_two_left() -> None:
    metas = [video_item(A, "2026-09-01T00:00:00Z"), video_item(B, "2026-09-14T00:00:00Z")]
    res = await make(_cmp_net(metas, {})).compare_videos((A, B), 7)
    assert res.status is ToolStatus.INSUFFICIENT_DATA
    assert "excluded:" + B + ":age_lt_7_days" in res.limitations


@pytest.mark.asyncio
async def test_compare_excludes_live_and_unknown_videos() -> None:
    metas = [
        video_item(A, "2026-09-01T00:00:00Z"),
        video_item(B, "2026-09-01T00:00:00Z"),
        video_item(C, "2026-09-01T00:00:00Z", live="live"),
    ]
    per = {A: _days("VIDEO_ON_DEMAND", [5] * 3), B: _days("VIDEO_ON_DEMAND", [6] * 3)}
    res = await make(_cmp_net(metas, per)).compare_videos((A, B, C, D), None)
    assert res.status is ToolStatus.OK
    assert res.data.excluded == {C: "live_or_upcoming", D: "not_found"}  # type: ignore[union-attr]


# ------------------------------------------------------------------ read-only / INV-20


@pytest.mark.asyncio
async def test_every_request_is_get_to_an_allowed_host_and_no_token_in_url() -> None:
    metas = [video_item(A, "2026-09-01T00:00:00Z"), video_item(B, "2026-09-01T00:00:00Z")]
    per = {A: _days("VIDEO_ON_DEMAND", [5] * 3), B: _days("VIDEO_ON_DEMAND", [6] * 3)}
    net = _cmp_net(metas, per)
    p = make(net)
    await p.compare_videos((A, B), None)
    await p.channel_performance(None, None)
    await p.audience_demographics(None, None, None)
    await p.audience_retention(VID, date(2026, 9, 1), date(2026, 9, 5))
    assert net.requests
    for r in net.requests:
        assert r.method == "GET"
        assert str(r.url).split("?")[0] in ALLOWED_GET
        assert "SECRET" not in str(r.url)
        assert r.headers["Authorization"] == "Bearer SECRET-TOK"
        assert r.content == b""


@pytest.mark.asyncio
async def test_no_write_or_monetary_scope_is_ever_required() -> None:
    from infrastructure.analytics import video_insights

    src = pathlib.Path(video_insights.__file__).read_text(encoding="utf-8")
    for forbidden in ("youtube.upload", "yt-analytics-monetary", "youtube.force-ssl", ".post("):
        assert forbidden not in src


def test_repr_of_provider_is_redacted() -> None:
    p = make(Net(lambda _r: httpx.Response(200)))
    assert "SECRET" not in repr(p) and "redacted" in repr(p)
    assert json.dumps(repr(p))
