"""YouTube Analytics API v2（``reports.query``）の adapter（ADR-0025）。

domain の ``AnalyticsProvider`` を実装する。MCP には依存しない
（旧 youtube-analytics-mcp は失われた）。

- 認証は upload と同じ ``RefreshTokenCredentials``（refresh token に ``yt-analytics.readonly``
  scope が要る。``scripts/youtube-oauth.py`` で同意し直す）
- 動画別の指標を窓（7 / 28 / 90 日）ごとに取る。視聴者構成（US・年齢・Shorts）は best-effort
- 通信・認証・応答の形のどの失敗も ``AnalyticsUnavailableError``（Planner は snapshot へ fallback）
- 例外文に token・URL の query を入れない（INV-20）
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from domain.errors import AnalyticsUnavailableError
from domain.topic_planning import AnalyticsReport, AudienceShares, VideoMetrics
from infrastructure.youtube.errors import YouTubeError
from infrastructure.youtube.oauth import RefreshTokenCredentials

REPORTS_ENDPOINT = "https://youtubeanalytics.googleapis.com/v2/reports"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
PROVIDER_ID = "youtube_analytics"

#: 動画別の指標（API の名前 → ``VideoMetrics`` の属性）
VIDEO_METRICS: dict[str, str] = {
    "views": "views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "averageViewDuration": "average_view_duration",
    "averageViewPercentage": "average_view_percentage",
    "likes": "likes",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
}
MAX_VIDEOS = 200
#: 視聴者構成を測る窓（日数）。要求された窓に無ければ最長の窓
AUDIENCE_WINDOW_DAYS = 28


def _yesterday() -> date:
    # Analytics は当日分が揃っていないので、前日までを範囲にする
    return datetime.now(UTC).date() - timedelta(days=1)


class YouTubeAnalyticsProvider:
    def __init__(
        self,
        credentials: RefreshTokenCredentials,
        *,
        client: httpx.AsyncClient,
        end_date: Callable[[], date] = _yesterday,
        endpoint: str = REPORTS_ENDPOINT,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self._end_date = end_date
        self._endpoint = endpoint

    def __repr__(self) -> str:
        return "YouTubeAnalyticsProvider(<redacted>)"

    async def fetch(self, window_days: AbstractSet[int]) -> AnalyticsReport:
        try:
            end = self._end_date()
            videos = {w: await self._videos(end, w) for w in sorted(window_days)}
            audience_window = (
                AUDIENCE_WINDOW_DAYS if AUDIENCE_WINDOW_DAYS in window_days else max(window_days)
            )
            audience = await self._audience(end, audience_window)
        except AnalyticsUnavailableError:
            raise
        except (_QueryRejected, *_BROKEN) as exc:
            raise AnalyticsUnavailableError(
                f"youtube analytics unavailable ({type(exc).__name__})"
            ) from None
        return AnalyticsReport(videos_by_window=videos, audience=audience)

    # ------------------------------------------------------------------ queries

    async def _videos(self, end: date, window: int) -> list[VideoMetrics]:
        rows = await self._query(
            end,
            window,
            metrics=",".join(VIDEO_METRICS),
            dimensions="video",
            sort="-views",
            maxResults=str(MAX_VIDEOS),
        )
        result: list[VideoMetrics] = []
        for row in rows:
            values = {attr: float(row[api]) for api, attr in VIDEO_METRICS.items()}
            result.append(VideoMetrics(video_id=str(row["video"]), **values))
        return result

    async def _audience(self, end: date, window: int) -> AudienceShares:
        """best-effort。項目ごとに取れなければ None（認証の拒否だけは全体を止める）。"""
        country_us = await self._optional(self._share(end, window, "country", "US"))
        ages = await self._optional(self._age_shares(end, window))
        shorts = await self._optional(self._share(end, window, "creatorContentType", "shorts"))
        return AudienceShares(
            country_us=country_us,
            age_18_24=ages.get("age18-24") if ages else None,
            age_25_34=ages.get("age25-34") if ages else None,
            shorts=shorts,
        )

    @staticmethod
    async def _optional(coro: Any) -> Any:
        try:
            return await coro
        except _CredentialsRejected:
            raise
        except (_QueryRejected, AnalyticsUnavailableError, *_BROKEN):
            return None

    async def _share(self, end: date, window: int, dimension: str, value: str) -> float | None:
        rows = await self._query(end, window, metrics="views", dimensions=dimension)
        total = sum(float(r["views"]) for r in rows)
        if total <= 0:
            return None
        hit = sum(float(r["views"]) for r in rows if str(r[dimension]).lower() == value.lower())
        return hit / total

    async def _age_shares(self, end: date, window: int) -> dict[str, float]:
        rows = await self._query(end, window, metrics="viewerPercentage", dimensions="ageGroup")
        shares: dict[str, float] = {}
        for r in rows:
            group = str(r["ageGroup"])
            shares[group] = shares.get(group, 0.0) + float(r["viewerPercentage"]) / 100.0
        return shares

    async def _query(self, end: date, window: int, **params: str) -> list[Mapping[str, Any]]:
        start = end - timedelta(days=window - 1)
        query = {
            "ids": "channel==MINE",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            **params,
        }
        response = await self._get(query)
        if response.status_code == 401:
            self._credentials.invalidate()
            response = await self._get(query)
        if response.status_code in (401, 403):
            # scope 未付与・権限なし: 人手の再同意が要る。どの query も通らない
            raise _CredentialsRejected(
                f"youtube analytics rejected credentials: HTTP {response.status_code}"
            )
        if response.status_code == 400:
            raise _QueryRejected(f"HTTP 400 for dimensions={params.get('dimensions')}")
        if response.status_code != 200:
            raise AnalyticsUnavailableError(
                f"youtube analytics failed: HTTP {response.status_code}"
            )
        body = response.json()
        headers = [h["name"] for h in body.get("columnHeaders", [])]
        return [dict(zip(headers, row, strict=True)) for row in body.get("rows") or []]

    async def _get(self, query: dict[str, str]) -> httpx.Response:
        token = await self._credentials.access_token()
        return await self._client.get(
            self._endpoint, params=query, headers={"Authorization": f"Bearer {token}"}
        )


class _QueryRejected(Exception):
    """query が 400 で拒否された（その次元が channel で使えない等）。"""


class _CredentialsRejected(AnalyticsUnavailableError):
    """401 / 403。scope 未付与などで、どの query も通らない。"""


#: 通信・token 更新・応答の形の失敗
_BROKEN: tuple[type[Exception], ...] = (
    YouTubeError,
    httpx.HTTPError,
    ValueError,
    KeyError,
    TypeError,
)
