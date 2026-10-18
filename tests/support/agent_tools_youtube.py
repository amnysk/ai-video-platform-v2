"""YouTube への通信を httpx.MockTransport で偽装する（実ネットワークに出ない / INV-18）。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
from pydantic import SecretStr

from infrastructure.analytics.video_insights import YouTubeVideoInsightProvider
from infrastructure.analytics.youtube_analytics import ANALYTICS_SCOPE, REPORTS_ENDPOINT
from infrastructure.youtube.data_reader import (
    CHANNELS_LIST_URL,
    DATA_READONLY_SCOPE,
    PLAYLIST_ITEMS_URL,
)
from infrastructure.youtube.oauth import (
    TOKEN_ENDPOINT,
    TOKENINFO_ENDPOINT,
    RefreshTokenCredentials,
)

TODAY = date(2026, 9, 20)
ALL_SCOPES = f"{ANALYTICS_SCOPE} {DATA_READONLY_SCOPE}"
DAILY = ["day", "creatorContentType", "views", "engagedViews", "estimatedMinutesWatched"]
DAILY += ["subscribersGained", "subscribersLost"]
TOTALS = ["views", "engagedViews", "estimatedMinutesWatched", "averageViewDuration"]
TOTALS += ["subscribersGained", "subscribersLost"]


def report(headers: list[str], rows: list[list[Any]]) -> httpx.Response:
    return httpx.Response(200, json={"columnHeaders": [{"name": h} for h in headers], "rows": rows})


class Net:
    """全リクエストを記録する。``requests`` の増減で「外部を呼んだか」を見る。"""

    def __init__(self, analytics: Callable[[httpx.Request], httpx.Response]) -> None:
        self.analytics = analytics
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        url = str(req.url).split("?")[0]
        if url == TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": "SECRET-TOK", "expires_in": 3600})
        if url == TOKENINFO_ENDPOINT:
            return httpx.Response(200, json={"scope": ALL_SCOPES})
        self.requests.append(req)
        if url == REPORTS_ENDPOINT:
            return self.analytics(req)
        if url == CHANNELS_LIST_URL:
            return httpx.Response(
                200, json={"items": [{"contentDetails": {"relatedPlaylists": {"uploads": "UUx"}}}]}
            )
        if url == PLAYLIST_ITEMS_URL:
            return httpx.Response(200, json={"items": [], "nextPageToken": None})
        return httpx.Response(404)


def channel_ok(req: httpx.Request) -> httpx.Response:
    if "dimensions" in req.url.params:
        return report(DAILY, [["2026-09-17", "SHORTS", 20, 15, 4, 3, 1]])
    return report(TOTALS, [[300, 200, 90, 22.0, 9, 2]])


def forbidden(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})


def make_provider(net: Net) -> YouTubeVideoInsightProvider:
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
