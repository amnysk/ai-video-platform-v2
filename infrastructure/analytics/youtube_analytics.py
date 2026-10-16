"""YouTube Analytics API v2（``reports.query``）の adapter（ADR-0025）。

domain の ``AnalyticsProvider`` を実装する。MCP には依存しない
（旧 youtube-analytics-mcp は失われた）。

- 認証は upload と同じ ``RefreshTokenCredentials``（refresh token に ``yt-analytics.readonly``
  scope が要る。``scripts/youtube-oauth.py`` で同意し直す）
- 動画別の指標を窓（7 / 28 / 90 日）ごとに取る。視聴者構成（US・年齢・Shorts）は best-effort
- 通信・認証・応答の形のどの失敗も ``AnalyticsUnavailableError``（Planner は snapshot へ fallback）
- 403 は tokeninfo で scope を確かめ、``yt-analytics.readonly`` が無ければ
  ``AnalyticsScopeMissingError``（「scripts/youtube-oauth.py で再同意」）にする
- 視聴者構成（年齢・性別・国・Shorts/長尺）は次元ごとに独立の best-effort。
  どの失敗（400 の非対応・403・5xx・形の崩れ）もその項目が None になるだけ
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
from infrastructure.youtube.oauth import TOKENINFO_ENDPOINT, RefreshTokenCredentials

REPORTS_ENDPOINT = "https://youtubeanalytics.googleapis.com/v2/reports"
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
PROVIDER_ID = "youtube_analytics"
#: upload 側が要る scope。再同意でも落とさない（scripts/youtube-oauth.py の SCOPES）
UPLOAD_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
SCOPE_MISSING_MESSAGE = (
    "scope missing: yt-analytics.readonly is not granted to the refresh token; "
    "re-run scripts/youtube-oauth.py"
)
#: 国別の構成比は上位だけ残す
MAX_COUNTRIES = 10

#: 動画別の指標（API の名前 → ``VideoMetrics`` の属性）
VIDEO_METRICS: dict[str, str] = {
    "views": "views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "averageViewDuration": "average_view_duration",
    "averageViewPercentage": "average_view_percentage",
    "likes": "likes",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
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
        tokeninfo_endpoint: str = TOKENINFO_ENDPOINT,
    ) -> None:
        self._tokeninfo_endpoint = tokeninfo_endpoint
        self._credentials = credentials
        self._client = client
        self._end_date = end_date
        self._endpoint = endpoint

    def __repr__(self) -> str:
        return "YouTubeAnalyticsProvider(<redacted>)"

    async def granted_scopes(self) -> frozenset[str]:
        """access token に付いた scope（tokeninfo）。token は URL に載せず form で送る。"""
        try:
            token = await self._credentials.access_token()
            response = await self._client.post(
                self._tokeninfo_endpoint, data={"access_token": token}
            )
        except (YouTubeError, httpx.HTTPError) as exc:
            raise AnalyticsUnavailableError(
                f"tokeninfo unavailable ({type(exc).__name__})"
            ) from None
        if response.status_code != 200:
            raise AnalyticsUnavailableError(f"tokeninfo failed: HTTP {response.status_code}")
        try:
            scope = response.json().get("scope", "")
        except (ValueError, AttributeError):
            raise AnalyticsUnavailableError("tokeninfo response is malformed") from None
        if not isinstance(scope, str):
            raise AnalyticsUnavailableError("tokeninfo response is malformed")
        return frozenset(scope.split())

    async def verify_scopes(self) -> frozenset[str]:
        """``yt-analytics.readonly`` が無ければ ``AnalyticsScopeMissingError``。"""
        scopes = await self.granted_scopes()
        if ANALYTICS_SCOPE not in scopes:
            raise AnalyticsScopeMissingError(SCOPE_MISSING_MESSAGE)
        return scopes

    async def fetch(self, window_days: AbstractSet[int]) -> AnalyticsReport:
        try:
            end = self._end_date()
            videos = {w: await self._videos(end, w) for w in sorted(window_days)}
            audience_window = (
                AUDIENCE_WINDOW_DAYS if AUDIENCE_WINDOW_DAYS in window_days else max(window_days)
            )
            audience = await self._audience(end, audience_window)
        except _CredentialsRejected as exc:
            if exc.status == 403:
                await self._diagnose_forbidden()
            raise
        except AnalyticsUnavailableError:
            raise
        except (_QueryRejected, *_BROKEN) as exc:
            raise AnalyticsUnavailableError(
                f"youtube analytics unavailable ({type(exc).__name__})"
            ) from None
        return AnalyticsReport(videos_by_window=videos, audience=audience)

    async def _diagnose_forbidden(self) -> None:
        """403 の原因が scope 不足なら、それと分かる例外にする。調べられなければ何もしない。"""
        try:
            scopes = await self.granted_scopes()
        except AnalyticsUnavailableError:
            return
        if ANALYTICS_SCOPE not in scopes:
            raise AnalyticsScopeMissingError(SCOPE_MISSING_MESSAGE)

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
        """best-effort。次元ごとに独立で、取れなければその項目だけ None。"""
        countries = await self._optional(self._shares(end, window, "country", "views"))
        ages = await self._optional(self._shares(end, window, "ageGroup", "viewerPercentage"))
        genders = await self._optional(self._shares(end, window, "gender", "viewerPercentage"))
        types = await self._optional(self._shares(end, window, "creatorContentType", "views"))
        return AudienceShares(
            country_us=_pick(countries, "US"),
            age_18_24=_pick(ages, "age18-24"),
            age_25_34=_pick(ages, "age25-34"),
            shorts=_pick(types, "SHORTS"),
            age_groups=ages,
            genders=genders,
            countries=_top(countries, MAX_COUNTRIES),
            content_types=types,
        )

    @staticmethod
    async def _optional(coro: Any) -> Any:
        try:
            return await coro
        except (_QueryRejected, AnalyticsUnavailableError, *_BROKEN):
            return None

    async def _shares(
        self, end: date, window: int, dimension: str, metric: str
    ) -> dict[str, float] | None:
        """次元の値ごとの構成比（0..1、合計 1）。行が無い・合計 0 なら None。"""
        rows = await self._query(end, window, metrics=metric, dimensions=dimension)
        totals: dict[str, float] = {}
        for r in rows:
            key = str(r[dimension])
            totals[key] = totals.get(key, 0.0) + float(r[metric])
        total = sum(totals.values())
        if total <= 0:
            return None
        return {k: v / total for k, v in totals.items()}

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
                f"youtube analytics rejected credentials: HTTP {response.status_code}",
                status=response.status_code,
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


class AnalyticsScopeMissingError(AnalyticsUnavailableError):
    """refresh token に ``yt-analytics.readonly`` が無い。人手の再同意が要る。"""


class _CredentialsRejected(AnalyticsUnavailableError):
    """401 / 403。scope 未付与などで、どの query も通らない。"""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


def _pick(shares: Mapping[str, float] | None, key: str) -> float | None:
    if not shares:
        return None
    wanted = key.lower()
    return sum(v for k, v in shares.items() if k.lower() == wanted)


def _top(shares: dict[str, float] | None, n: int) -> dict[str, float] | None:
    if shares is None:
        return None
    return dict(sorted(shares.items(), key=lambda kv: (-kv[1], kv[0]))[:n])


#: 通信・token 更新・応答の形の失敗
_BROKEN: tuple[type[Exception], ...] = (
    YouTubeError,
    httpx.HTTPError,
    ValueError,
    KeyError,
    TypeError,
)
