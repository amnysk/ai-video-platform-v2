"""YouTube Data API v3 の検索（``SearchProvider`` 実装、ADR-0031 §7 / ADR-0033 §5）。

**worker へは配線しない**（research-worker は ``YOUTUBE_*`` を持たない。ADR-0031 §8）。
有効化には ADR・env の追加・``youtube.readonly`` scope の同意確認が要る。エンドポイントの文字列が
``infrastructure/youtube/`` にだけ置けるのは INV-18 の規則
（``tests/architecture/test_no_live_calls.py``）。

呼び出し（quota 単位は公開 docs の値で**この repo では未検証**。
``infrastructure/research/quota_costs.py``）:

1. ``search.list``（100 units）: 動画の ID と snippet
2. ``videos.list``（1 unit / バッチ）: 統計・再生時間。``observed_at`` はこの応答を受けた時刻
3. ``channels.list``（1 unit / バッチ）: 購読者数。**取れれば**（失敗・非公開は欠けたまま）

``SearchResults.cost_units`` は**送った呼び出し**の合計（失敗した補助呼び出しも課金された
ものとして数える。INV-15 の保守的な扱い）。``search.list`` が例外になったときは値を返せない
ので、呼び出し側は最大の単位が消費されたと見なす。

**意味の断定をしない**:

- ``regionCode`` は「その地域で視聴可能な動画」への**絞り込み**、``relevanceLanguage`` は言語への
  関連度の**重みづけ**であって、人気・視聴者層・動画の言語の断定ではない。指定した値は
  ``extras`` に ``requested_region_code`` / ``requested_relevance_language`` として残すだけ。
- ``videoDuration`` は使わない。``short`` は「4 分未満」の条件であって Shorts の判定ではない
  （ADR-0033 (d)）。再生時間は ``duration_seconds`` として事実だけを返す。
- 非公開・欠けた統計は 0 にせず欠けたままにする。
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from domain.research.ports import SearchHit, SearchQuery, SearchResults, StatObservation
from infrastructure.research.quota_costs import (
    YOUTUBE_CHANNELS_LIST_UNITS,
    YOUTUBE_SEARCH_LIST_UNITS,
    YOUTUBE_VIDEOS_LIST_UNITS,
)
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeError,
    YouTubeTransientError,
)
from infrastructure.youtube.uploader import API_BASE_URL, _raise_for_error

SEARCH_URL = f"{API_BASE_URL}/search"
VIDEOS_URL = f"{API_BASE_URL}/videos"
CHANNELS_URL = f"{API_BASE_URL}/channels"
MAX_PAGE_SIZE = 50
WATCH_URL = "https://www.youtube.com/watch?v={video_id}"
PROVIDER_ID = "youtube"

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


class TokenSource(Protocol):
    """``RefreshTokenCredentials`` が満たす。access token は例外・ログに出さない。"""

    async def access_token(self) -> str: ...

    def invalidate(self) -> None: ...


def parse_iso8601_duration(value: str | None) -> int | None:
    """``PT10M12S`` → 612。読めなければ ``None``（推測しない）。"""
    if not value:
        return None
    match = _DURATION.match(value)
    if match is None or all(g is None for g in match.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime filters must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: object) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


class YouTubeSearchProvider:
    """``domain.research.ports.SearchProvider`` の YouTube 実装。"""

    name = PROVIDER_ID

    def __init__(
        self,
        credentials: TokenSource,
        *,
        client: httpx.AsyncClient,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return "YouTubeSearchProvider()"

    async def search(self, query: SearchQuery) -> SearchResults:
        if query.kind != "youtube":
            raise ValueError("YouTubeSearchProvider only handles kind='youtube' queries")
        if not query.text.strip() or query.max_results < 1:
            raise ValueError("query needs text and max_results >= 1")
        params: dict[str, str] = {
            "part": "snippet",
            "type": "video",
            "q": query.text,
            "maxResults": str(min(query.max_results, MAX_PAGE_SIZE)),
        }
        # 絞り込み・重みづけであって断定ではない（モジュール docstring）
        if query.region_code:
            params["regionCode"] = query.region_code
        if query.language:
            params["relevanceLanguage"] = query.language
        if query.published_after:
            params["publishedAfter"] = _rfc3339(query.published_after)
        if query.published_before:
            params["publishedBefore"] = _rfc3339(query.published_before)
        # 意図的に videoDuration を送らない（Shorts の判定に使わない）

        cost = YOUTUBE_SEARCH_LIST_UNITS
        found = await self._get(SEARCH_URL, params, "search.list")
        items = [i for i in found.get("items") or [] if isinstance(i, Mapping)]
        entries: list[tuple[str, Mapping[str, Any]]] = []
        for item in items:
            video_id = _mapping(item.get("id")).get("videoId")
            if isinstance(video_id, str) and video_id:
                entries.append((video_id, _mapping(item.get("snippet"))))
        truncated = bool(found.get("nextPageToken")) or query.max_results > MAX_PAGE_SIZE
        warnings: list[str] = []
        details: dict[str, Mapping[str, Any]] = {}
        observed_at = self._clock()

        if entries:
            ids = list(dict.fromkeys(v for v, _ in entries))
            cost += YOUTUBE_VIDEOS_LIST_UNITS
            try:
                body = await self._get(
                    VIDEOS_URL,
                    {"part": "snippet,contentDetails,statistics", "id": ",".join(ids)},
                    "videos.list",
                )
                observed_at = self._clock()  # 統計を観測したのはこの応答の時点
                details = {
                    str(v.get("id")): v for v in body.get("items") or [] if isinstance(v, Mapping)
                }
                missing = [v for v in ids if v not in details]
                if missing:
                    warnings.append(f"video_details_missing:{','.join(missing)}")
            except YouTubeError as exc:
                warnings.append(f"videos_list_failed:{type(exc).__name__}")

        subscribers: dict[str, int | None] = {}
        channel_ids = list(
            dict.fromkeys(str(s["channelId"]) for _, s in entries if s.get("channelId"))
        )
        if channel_ids:
            cost += YOUTUBE_CHANNELS_LIST_UNITS
            try:
                body = await self._get(
                    CHANNELS_URL,
                    {"part": "statistics", "id": ",".join(channel_ids)},
                    "channels.list",
                )
                for channel in body.get("items") or []:
                    stats = _mapping(_mapping(channel).get("statistics"))
                    hidden = stats.get("hiddenSubscriberCount") is True
                    subscribers[str(_mapping(channel).get("id"))] = (
                        None if hidden else _count(stats.get("subscriberCount"))
                    )
            except YouTubeError as exc:
                warnings.append(f"channels_list_failed:{type(exc).__name__}")

        hits = tuple(
            self._hit(video_id, snippet, details.get(video_id), subscribers, observed_at, query)
            for video_id, snippet in _unique(entries)
        )
        return SearchResults(
            query=query,
            hits=hits,
            searched_at=self._clock(),
            provider=self.name,
            cost_units=cost,
            truncated=truncated,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _hit(
        video_id: str,
        snippet: Mapping[str, Any],
        detail: Mapping[str, Any] | None,
        subscribers: Mapping[str, int | None],
        observed_at: datetime,
        query: SearchQuery,
    ) -> SearchHit:
        detail = detail or {}
        statistics = _mapping(detail.get("statistics"))
        stats = tuple(
            StatObservation(metric, value, "count", observed_at)
            for metric, key in (
                ("view_count", "viewCount"),
                ("like_count", "likeCount"),
                ("comment_count", "commentCount"),
            )
            if (value := _count(statistics.get(key))) is not None
        )
        extras: dict[str, str] = {}
        if query.region_code:
            extras["requested_region_code"] = query.region_code
        if query.language:
            extras["requested_relevance_language"] = query.language
        channel_id = snippet.get("channelId")
        return SearchHit(
            url=WATCH_URL.format(video_id=video_id),
            title=html.unescape(str(snippet.get("title") or "")),
            snippet=html.unescape(str(snippet.get("description") or "")),
            published_at=_parse_time(snippet.get("publishedAt")),
            provider=PROVIDER_ID,
            provider_ref=video_id,
            channel_id=str(channel_id) if channel_id else None,
            channel_title=str(snippet["channelTitle"]) if snippet.get("channelTitle") else None,
            stats=stats,
            channel_subscriber_count=subscribers.get(str(channel_id)) if channel_id else None,
            duration_seconds=parse_iso8601_duration(
                _mapping(detail.get("contentDetails")).get("duration")
            ),
            extras=extras,
        )

    async def _get(self, url: str, params: Mapping[str, str], operation: str) -> Mapping[str, Any]:
        """Bearer を付けて GET。401 なら token を更新して 1 回だけ再送する。"""
        response: httpx.Response | None = None
        for _ in (0, 1):
            token = await self._credentials.access_token()
            try:
                response = await self._client.get(
                    url, params=params, headers={"Authorization": f"Bearer {token}"}
                )
            except httpx.TransportError as exc:
                raise YouTubeTransientError(
                    f"{operation}: transport failure ({type(exc).__name__})"
                ) from None
            if response.status_code != 401:
                break
            self._credentials.invalidate()
        assert response is not None
        if response.status_code == 401:
            raise YouTubeAuthError(f"{operation}: HTTP 401 after token refresh")
        if response.status_code != 200:
            _raise_for_error(response, operation)
        try:
            body = response.json()
        except ValueError:
            raise YouTubeTransientError(f"{operation}: response is not json") from None
        if not isinstance(body, Mapping):
            raise YouTubeTransientError(f"{operation}: unexpected response shape")
        return body


def _unique(
    entries: Sequence[tuple[str, Mapping[str, Any]]],
) -> list[tuple[str, Mapping[str, Any]]]:
    seen: dict[str, Mapping[str, Any]] = {}
    for video_id, snippet in entries:
        seen.setdefault(video_id, snippet)
    return list(seen.items())
