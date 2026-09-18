"""YouTube Analytics adapter（ADR-0025）。

httpx.MockTransport だけ（ネットワークに出ない / INV-18）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from domain.errors import AnalyticsUnavailableError
from infrastructure.analytics.youtube_analytics import (
    REPORTS_ENDPOINT,
    VIDEO_METRICS,
    YouTubeAnalyticsProvider,
)
from infrastructure.youtube.oauth import TOKEN_ENDPOINT, RefreshTokenCredentials

END = date(2026, 9, 17)


def _report(headers: list[str], rows: list[list[Any]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "columnHeaders": [{"name": h} for h in headers],
            "rows": rows,
        },
    )


def _video_rows(n: int, views: float = 100.0) -> list[list[Any]]:
    return [[f"vid{i}", views * (i + 1), 10, 30, 75.5, 3, 1, 0] for i in range(n)]


def default_handler(req: httpx.Request) -> httpx.Response:
    dims = req.url.params.get("dimensions")
    if dims == "video":
        return _report(["video", *VIDEO_METRICS], _video_rows(3))
    if dims == "country":
        return _report(["country", "views"], [["US", 60], ["JP", 40]])
    if dims == "ageGroup":
        return _report(
            ["ageGroup", "viewerPercentage"],
            [["age18-24", 30.0], ["age25-34", 25.0], ["age35-44", 45.0]],
        )
    if dims == "creatorContentType":
        return _report(["creatorContentType", "views"], [["SHORTS", 90], ["VIDEO_ON_DEMAND", 10]])
    return httpx.Response(400)


class Recorder:
    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if str(req.url) == TOKEN_ENDPOINT:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        self.requests.append(req)
        return self.handler(req)


def provider(rec: Recorder) -> YouTubeAnalyticsProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(rec))
    creds = RefreshTokenCredentials(
        "client-id", SecretStr("secret"), SecretStr("refresh"), client=client
    )
    return YouTubeAnalyticsProvider(creds, client=client, end_date=lambda: END)


@pytest.mark.asyncio
async def test_fetches_per_video_metrics_for_each_window_and_audience() -> None:
    rec = Recorder(default_handler)
    report = await provider(rec).fetch({7, 28, 90})
    assert set(report.videos_by_window) == {7, 28, 90}
    vids = report.videos_by_window[28]
    assert [v.video_id for v in vids] == ["vid0", "vid1", "vid2"]
    assert vids[1].views == 200.0
    assert vids[0].average_view_percentage == 75.5
    assert report.audience.country_us == pytest.approx(0.6)
    assert report.audience.age_18_24 == pytest.approx(0.30)
    assert report.audience.age_25_34 == pytest.approx(0.25)
    assert report.audience.shorts == pytest.approx(0.9)

    video_queries = [r for r in rec.requests if r.url.params.get("dimensions") == "video"]
    assert len(video_queries) == 3
    q = {r.url.params["startDate"]: r for r in video_queries}
    assert set(q) == {"2026-09-11", "2026-08-21", "2026-06-20"}
    first = video_queries[0]
    assert str(first.url).startswith(REPORTS_ENDPOINT)
    assert first.url.params["ids"] == "channel==MINE"
    assert first.url.params["endDate"] == "2026-09-17"
    assert first.url.params["sort"] == "-views"
    assert first.url.params["maxResults"] == "200"
    assert first.url.params["metrics"].split(",") == list(VIDEO_METRICS)
    assert first.headers["Authorization"] == "Bearer tok"
    assert rec.token_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 500, 429])
async def test_http_errors_become_analytics_unavailable(status: int) -> None:
    rec = Recorder(lambda _r: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(AnalyticsUnavailableError) as info:
        await provider(rec).fetch({28})
    assert "tok" not in str(info.value)


@pytest.mark.asyncio
async def test_401_refreshes_the_token_once_then_gives_up() -> None:
    rec = Recorder(lambda _r: httpx.Response(401))
    with pytest.raises(AnalyticsUnavailableError):
        await provider(rec).fetch({28})
    assert rec.token_calls == 2
    assert len(rec.requests) == 2


@pytest.mark.asyncio
async def test_token_refresh_failure_is_analytics_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    creds = RefreshTokenCredentials("id", SecretStr("s"), SecretStr("r"), client=client)
    with pytest.raises(AnalyticsUnavailableError):
        await YouTubeAnalyticsProvider(creds, client=client, end_date=lambda: END).fetch({28})


@pytest.mark.asyncio
async def test_transport_error_is_analytics_unavailable() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    rec = Recorder(handler)
    with pytest.raises(AnalyticsUnavailableError):
        await provider(rec).fetch({28})


@pytest.mark.asyncio
async def test_malformed_body_is_analytics_unavailable() -> None:
    rec = Recorder(
        lambda _r: httpx.Response(
            200, json={"columnHeaders": [{"name": "video"}], "rows": [["a", 1]]}
        )
    )
    with pytest.raises(AnalyticsUnavailableError):
        await provider(rec).fetch({28})


@pytest.mark.asyncio
async def test_audience_is_best_effort() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("dimensions") == "video":
            return default_handler(req)
        if req.url.params.get("dimensions") == "ageGroup":
            return httpx.Response(503)
        return httpx.Response(400)

    report = await provider(Recorder(handler)).fetch({28})
    assert len(report.videos_by_window[28]) == 3
    assert report.audience.country_us is None
    assert report.audience.age_18_24 is None
    assert report.audience.shorts is None


@pytest.mark.asyncio
async def test_empty_channel_gives_empty_lists() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"columnHeaders": [{"name": "video"}]})

    report = await provider(Recorder(handler)).fetch({7})
    assert report.videos_by_window == {7: []}
