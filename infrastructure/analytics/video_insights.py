"""動画別 insight の adapter（ADR-0047 §5）。``YouTubeAnalyticsProvider`` の拡張。

- 認証・tokeninfo・``reports.query`` の URL は既存 adapter のものをそのまま使う（クラスを継承）。
  ``AnalyticsProvider.fetch`` と ``analytics_snapshots`` の経路には触れない
- 通信は **GET のみ**（tokeninfo の form POST は既存 ``granted_scopes`` が行う）。
  書き込み・収益 scope は要求しない（INV-61）
- 失敗は例外ではなく ``InsightResult.status``（``ToolStatus``）。token・URL の query・応答本文を
  どの結果・例外・repr にも出さない（INV-20）
- 取得できない値を 0 で埋めない（INV-58）。quota 単価は公式に見つからないので ``unknown``

状態の対応:
  200 + 行なし = no_data / 年齢・国など閾値次元で行なし = withheld_by_threshold /
  日別の最終日が要求より前 or 範囲が処理遅延内 = reporting_delay /
  403 quotaExceeded = quota_exceeded / 403 rateLimitExceeded・429 = rate_limited /
  その他の 403 = tokeninfo で scope を確認し permission_denied / 400 = failed(invalid_query)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel

from contracts.agent_tools import ToolStatus, input_sha256
from contracts.agent_tools_analytics import (
    SHORTS_VIEWS_REDEFINITION_UNTIL,
    UNAVAILABLE_METRICS,
    AgeGenderShare,
    ChannelOutput,
    CompareGroup,
    CompareOutput,
    CompareVideo,
    CountryShare,
    DailySubscribers,
    DemographicsOutput,
    FormatTotals,
    RetentionOutput,
    RetentionPoint,
    VideoAnalyticsOutput,
)
from domain.analytics.derive import (
    age_days,
    aligned_window,
    largest_drops,
    shorts_views_affected,
    weighted_mean,
)
from domain.analytics.ports import InsightResult
from domain.errors import AnalyticsUnavailableError
from infrastructure.analytics.youtube_analytics import (
    ANALYTICS_SCOPE,
    REPORTS_ENDPOINT,
    YouTubeAnalyticsProvider,
)
from infrastructure.youtube.data_reader import (
    CHANNELS_LIST_URL,
    DATA_READONLY_SCOPE,
    PLAYLIST_ITEMS_URL,
    VIDEOS_LIST_URL,
    VideoMeta,
    parse_playlist_page,
    parse_uploads_playlist,
    parse_videos,
)
from infrastructure.youtube.errors import YouTubeAuthError, YouTubeError
from infrastructure.youtube.oauth import TOKENINFO_ENDPOINT, RefreshTokenCredentials

#: 直近この日数は未確定として扱う（公式は 48〜72 時間程度の遅延。厳密な値は公式から確認できない）
PROCESSING_DELAY_DAYS = 3
DEFAULT_WINDOW_DAYS = 28
MAX_RANGE_DAYS = 366
#: compare: これ未満の日数では比べない / 最短と最長の経過日数の比がこれを超えたら比べない
MIN_ALIGNED_DAYS = 3
MAX_AGE_RATIO = 10
MAX_ALIGNED_DAYS = 90
MAX_DROPS = 3
MAX_COUNTRIES = 10
#: 投稿数の計数で読む playlistItems の最大ページ数（1 ページ 50 件・1 unit）
MAX_PLAYLIST_PAGES = 10

VIDEO_METRIC_COLUMNS: dict[str, str] = {
    "views": "views",
    "engagedViews": "engaged_views",
    "estimatedMinutesWatched": "estimated_minutes_watched",
    "averageViewDuration": "average_view_duration_seconds",
    "likes": "likes",
    "comments": "comments",
    "subscribersGained": "subscribers_gained",
}
COMPARE_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
)
CHANNEL_TOTAL_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "subscribersGained",
    "subscribersLost",
)
CHANNEL_DAILY_METRICS = (
    "views",
    "engagedViews",
    "estimatedMinutesWatched",
    "subscribersGained",
    "subscribersLost",
)

_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})


class _Stop(Exception):
    """tool の結果を状態で打ち切る内部の合図。外へは出ない。"""

    def __init__(self, status: ToolStatus, code: str, limitations: tuple[str, ...] = ()) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.limitations = limitations


@dataclass
class _Call:
    """1 回の tool 呼び出しの記録（証跡の hash と limitations）。"""

    queries: list[dict[str, str]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def note(self, code: str) -> None:
        if code not in self.limitations:
            self.limitations.append(code)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _day(value: Any) -> date:
    return date.fromisoformat(str(value))


class YouTubeVideoInsightProvider(YouTubeAnalyticsProvider):
    def __init__(
        self,
        credentials: RefreshTokenCredentials,
        *,
        client: httpx.AsyncClient,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        endpoint: str = REPORTS_ENDPOINT,
        tokeninfo_endpoint: str = TOKENINFO_ENDPOINT,
    ) -> None:
        super().__init__(
            credentials, client=client, endpoint=endpoint, tokeninfo_endpoint=tokeninfo_endpoint
        )
        self._today = today
        self._now = now

    def __repr__(self) -> str:
        return "YouTubeVideoInsightProvider(<redacted>)"

    def confirmed_end(self) -> date:
        return self._today() - timedelta(days=PROCESSING_DELAY_DAYS)

    # ------------------------------------------------------------ public (port)

    async def video_analytics(
        self,
        video_id: str,
        start: date | None,
        end: date | None,
        metrics: tuple[str, ...] | None,
    ) -> InsightResult[VideoAnalyticsOutput]:
        async def run(call: _Call) -> InsightResult[VideoAnalyticsOutput]:
            wanted = metrics or tuple(VIDEO_METRIC_COLUMNS)
            if any(m in UNAVAILABLE_METRICS for m in wanted):
                raise _Stop(
                    ToolStatus.NOT_AVAILABLE_VIA_API,
                    "metric_not_in_api",
                    tuple(f"not_available_via_api:{m}" for m in wanted if m in UNAVAILABLE_METRICS),
                )
            published_at: str | None = None
            first = start
            if first is None:
                meta = (await self._video_metas(call, (video_id,))).get(video_id)
                if meta is None:
                    raise _Stop(ToolStatus.NO_DATA, "video_not_found")
                published_at = meta.published_at
                first = meta.published_date
            first, last = self._resolve(first, end, default_start=None, call=call)
            self._shorts_note(call, first)
            rows = await self._report(
                call,
                startDate=first.isoformat(),
                endDate=last.isoformat(),
                metrics=",".join(wanted),
                filters=f"video=={video_id}",
            )
            if not rows:
                return self._done(call, ToolStatus.NO_DATA)
            row = rows[0]
            values = {VIDEO_METRIC_COLUMNS[m]: _num(row.get(m)) for m in wanted if m in row}
            data = VideoAnalyticsOutput(
                video_id=video_id,
                start_date=first,
                end_date=last,
                published_at=published_at,
                **values,
            )
            return self._done(call, ToolStatus.OK, data)

        return await self._guard(run)

    async def audience_retention(
        self, video_id: str, start: date | None, end: date | None
    ) -> InsightResult[RetentionOutput]:
        async def run(call: _Call) -> InsightResult[RetentionOutput]:
            first = start
            if first is None:
                meta = (await self._video_metas(call, (video_id,))).get(video_id)
                if meta is None:
                    raise _Stop(ToolStatus.NO_DATA, "video_not_found")
                first = meta.published_date
            first, last = self._resolve(first, end, default_start=None, call=call)
            rows = await self._report(
                call,
                startDate=first.isoformat(),
                endDate=last.isoformat(),
                dimensions="elapsedVideoTimeRatio",
                metrics="audienceWatchRatio,relativeRetentionPerformance",
                filters=f"video=={video_id}",
            )
            if not rows:
                return self._done(call, ToolStatus.NO_DATA)
            points = sorted(
                (
                    RetentionPoint(
                        position_ratio=float(r["elapsedVideoTimeRatio"]),
                        audience_watch_ratio=_num(r.get("audienceWatchRatio")),
                        relative_retention_performance=_num(r.get("relativeRetentionPerformance")),
                    )
                    for r in rows
                ),
                key=lambda p: p.position_ratio,
            )
            drops = largest_drops(
                [(p.position_ratio, p.audience_watch_ratio) for p in points], k=MAX_DROPS
            )
            call.note("largest_drops_is_derived")
            data = RetentionOutput(
                video_id=video_id,
                start_date=first,
                end_date=last,
                points=tuple(points),
                largest_drops=drops,
            )
            return self._done(call, ToolStatus.OK, data)

        return await self._guard(run)

    async def audience_demographics(
        self, video_id: str | None, start: date | None, end: date | None
    ) -> InsightResult[DemographicsOutput]:
        async def run(call: _Call) -> InsightResult[DemographicsOutput]:
            first, last = self._resolve(start, end, default_start=DEFAULT_WINDOW_DAYS, call=call)
            flt = {"filters": f"video=={video_id}"} if video_id else {}
            window = {"startDate": first.isoformat(), "endDate": last.isoformat()}
            age_rows = await self._report(
                call,
                **window,
                dimensions="ageGroup,gender",
                metrics="viewerPercentage",
                **flt,
            )
            country_rows = await self._report(
                call, **window, dimensions="country", metrics="views", **flt
            )
            age_gender = tuple(
                AgeGenderShare(
                    age_group=str(r["ageGroup"]),
                    gender=str(r["gender"]),
                    viewer_percentage=float(r["viewerPercentage"]),
                )
                for r in age_rows
            )
            total = sum(float(r["views"]) for r in country_rows)
            countries = tuple(
                CountryShare(country=str(r["country"]), share_of_views=float(r["views"]) / total)
                for r in sorted(country_rows, key=lambda r: (-float(r["views"]), str(r["country"])))
                if total > 0
            )[:MAX_COUNTRIES]
            call.note("viewer_percentage_not_summable_across_dimensions")
            call.note("country_share_of_views_is_derived")
            if not age_gender:
                call.note("age_gender_withheld_by_threshold")
            if not countries:
                call.note("countries_withheld_by_threshold")
            if not age_gender and not countries:
                return self._done(call, ToolStatus.WITHHELD_BY_THRESHOLD)
            data = DemographicsOutput(
                video_id=video_id,
                start_date=first,
                end_date=last,
                age_gender=age_gender or None,
                countries=countries or None,
            )
            return self._done(call, ToolStatus.OK, data)

        return await self._guard(run)

    async def channel_performance(
        self, start: date | None, end: date | None
    ) -> InsightResult[ChannelOutput]:
        async def run(call: _Call) -> InsightResult[ChannelOutput]:
            first, last = self._resolve(start, end, default_start=DEFAULT_WINDOW_DAYS, call=call)
            self._shorts_note(call, first)
            window = {"startDate": first.isoformat(), "endDate": last.isoformat()}
            totals = await self._report(call, **window, metrics=",".join(CHANNEL_TOTAL_METRICS))
            daily = await self._report(
                call,
                **window,
                dimensions="day,creatorContentType",
                metrics=",".join(CHANNEL_DAILY_METRICS),
            )
            if not totals and not daily:
                return self._done(call, ToolStatus.NO_DATA)
            if daily:
                last_day = max(_day(r["day"]) for r in daily)
                if last_day < last:
                    raise _Stop(
                        ToolStatus.REPORTING_DELAY,
                        "response_ends_before_requested_end",
                        (f"last_day_returned:{last_day.isoformat()}",),
                    )
            by_format: dict[str, dict[str, float]] = {}
            per_day: dict[date, list[float]] = {}
            for r in daily:
                bucket = by_format.setdefault(str(r["creatorContentType"]), {})
                for m in CHANNEL_DAILY_METRICS:
                    bucket[m] = bucket.get(m, 0.0) + (_num(r.get(m)) or 0.0)
                g, lo = per_day.setdefault(_day(r["day"]), [0.0, 0.0])[:2]
                per_day[_day(r["day"])] = [
                    g + (_num(r.get("subscribersGained")) or 0.0),
                    lo + (_num(r.get("subscribersLost")) or 0.0),
                ]
            count, truncated = await self._published_count(call, first, last)
            t = totals[0] if totals else {}
            data = ChannelOutput(
                start_date=first,
                end_date=last,
                views=_num(t.get("views")),
                engaged_views=_num(t.get("engagedViews")),
                estimated_minutes_watched=_num(t.get("estimatedMinutesWatched")),
                average_view_duration_seconds=_num(t.get("averageViewDuration")),
                subscribers_gained=_num(t.get("subscribersGained")),
                subscribers_lost=_num(t.get("subscribersLost")),
                by_format=tuple(
                    FormatTotals(
                        content_type=name,
                        views=v["views"],
                        engaged_views=v["engagedViews"],
                        estimated_minutes_watched=v["estimatedMinutesWatched"],
                        subscribers_gained=v["subscribersGained"],
                        subscribers_lost=v["subscribersLost"],
                    )
                    for name, v in sorted(by_format.items())
                ),
                daily_subscribers=tuple(
                    DailySubscribers(date=d, subscribers_gained=g, subscribers_lost=lo)
                    for d, (g, lo) in sorted(per_day.items())
                ),
                published_count=count,
                published_count_truncated=truncated,
            )
            call.note("published_count_not_split_by_format")
            return self._done(call, ToolStatus.OK, data)

        return await self._guard(run)

    async def compare_videos(
        self, video_ids: tuple[str, ...], days: int | None
    ) -> InsightResult[CompareOutput]:
        async def run(call: _Call) -> InsightResult[CompareOutput]:
            metas = await self._video_metas(call, video_ids)
            excluded: dict[str, str] = {}
            eligible: list[VideoMeta] = []
            confirmed = self.confirmed_end()
            for vid in video_ids:
                meta = metas.get(vid)
                if meta is None:
                    excluded[vid] = "not_found"
                elif meta.live_broadcast_content != "none":
                    excluded[vid] = "live_or_upcoming"
                else:
                    eligible.append(meta)
            ages = {m.video_id: age_days(m.published_date, confirmed) for m in eligible}
            if days is not None:
                for m in list(eligible):
                    if ages[m.video_id] < days:
                        excluded[m.video_id] = f"age_lt_{days}_days"
                        eligible.remove(m)
            if len(eligible) < 2:
                raise _Stop(
                    ToolStatus.INSUFFICIENT_DATA,
                    "fewer_than_2_comparable_videos",
                    tuple(f"excluded:{k}:{v}" for k, v in sorted(excluded.items())),
                )
            min_age = min(ages[m.video_id] for m in eligible)
            if days is None and max(ages[m.video_id] for m in eligible) > MAX_AGE_RATIO * max(
                min_age, 1
            ):
                raise _Stop(ToolStatus.INSUFFICIENT_DATA, "ages_differ_too_much")
            n = days if days is not None else min(min_age, MAX_ALIGNED_DAYS)
            if n < MIN_ALIGNED_DAYS:
                raise _Stop(ToolStatus.INSUFFICIENT_DATA, "window_too_short")

            rows_by_type: dict[str, list[tuple[CompareVideo, date]]] = {}
            for meta in eligible:
                first, last = aligned_window(meta.published_date, n)
                rows = await self._report(
                    call,
                    startDate=first.isoformat(),
                    endDate=last.isoformat(),
                    dimensions="day,creatorContentType",
                    metrics=",".join(COMPARE_METRICS),
                    filters=f"video=={meta.video_id}",
                )
                if not rows:
                    excluded[meta.video_id] = "no_data"
                    continue
                kind, video = _aggregate(meta, rows)
                rows_by_type.setdefault(kind, []).append((video, first))

            groups = tuple(
                _group(kind, entries, n) for kind, entries in sorted(rows_by_type.items())
            )
            if not any(g.status == "ok" for g in groups):
                raise _Stop(
                    ToolStatus.INSUFFICIENT_DATA,
                    "no_group_has_2_comparable_videos",
                    tuple(f"excluded:{k}:{v}" for k, v in sorted(excluded.items())),
                )
            for code in (
                "compare_never_ranks_across_content_types",
                "window_averages_derived_views_weighted",
                "day_boundary_timezone_unverified",
                "publish_day_may_be_partial",
            ):
                call.note(code)
            if any(g.views_status == "insufficient_data" for g in groups):
                call.note(
                    f"views_insufficient_data_shorts_definition_change_until_"
                    f"{SHORTS_VIEWS_REDEFINITION_UNTIL.isoformat()}"
                )
            return self._done(
                call,
                ToolStatus.OK,
                CompareOutput(window_days=n, groups=groups, excluded=dict(excluded)),
            )

        return await self._guard(run)

    # ------------------------------------------------------------ plumbing

    def _done(
        self, call: _Call, status: ToolStatus, data: BaseModel | None = None
    ) -> InsightResult[Any]:
        return InsightResult(
            status=status,
            data=data,
            limitations=tuple(call.limitations),
            evidence_refs=(f"analytics_query:{input_sha256({'queries': call.queries})}",),
            retrieved_at=self._now().isoformat(),
        )

    async def _guard[T: BaseModel](
        self, run: Callable[[_Call], Awaitable[InsightResult[T]]]
    ) -> InsightResult[T]:
        call = _Call()
        try:
            return await run(call)
        except _Stop as stop:
            out = self._done(call, stop.status)
            return InsightResult(
                status=out.status,
                limitations=(*call.limitations, *stop.limitations),
                evidence_refs=out.evidence_refs if call.queries else (),
                retrieved_at=out.retrieved_at,
                error_code=stop.code,
            )
        except (KeyError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            out = self._done(call, ToolStatus.FAILED)
            return InsightResult(
                status=ToolStatus.FAILED,
                limitations=out.limitations,
                evidence_refs=out.evidence_refs if call.queries else (),
                retrieved_at=out.retrieved_at,
                error_code="malformed_response",
            )

    def _resolve(
        self, start: date | None, end: date | None, *, default_start: int | None, call: _Call
    ) -> tuple[date, date]:
        confirmed = self.confirmed_end()
        last = end if end is not None else confirmed
        if last > confirmed:
            raise _Stop(
                ToolStatus.REPORTING_DELAY,
                "range_within_processing_window",
                (f"latest_confirmed_date:{confirmed.isoformat()}",),
            )
        if start is None:
            if default_start is None:  # unreachable: callers pass a start
                raise _Stop(ToolStatus.FAILED, "invalid_range")
            start = last - timedelta(days=default_start - 1)
        if start > last:
            # 公開直後など、確定した日がまだ無い
            raise _Stop(
                ToolStatus.REPORTING_DELAY,
                "no_confirmed_day_in_range",
                (f"latest_confirmed_date:{confirmed.isoformat()}",),
            )
        if (last - start).days + 1 > MAX_RANGE_DAYS:
            raise _Stop(ToolStatus.FAILED, "range_too_long")
        call.note("day_boundary_timezone_unverified")
        return start, last

    @staticmethod
    def _shorts_note(call: _Call, start: date) -> None:
        if shorts_views_affected(start):
            call.note("shorts_views_definition_changed_2025_03_31")

    async def _report(self, call: _Call, **params: str) -> list[dict[str, Any]]:
        query = {"ids": "channel==MINE", **params}
        call.queries.append({"api": "reports.query", **query})
        body = await self._request(self._endpoint, query, ANALYTICS_SCOPE)
        headers = [h["name"] for h in body.get("columnHeaders", [])]
        return [dict(zip(headers, row, strict=True)) for row in body.get("rows") or []]

    async def _data(self, call: _Call, url: str, name: str, **params: str) -> Any:
        call.queries.append({"api": name, **params})
        return await self._request(url, params, DATA_READONLY_SCOPE)

    async def _video_metas(self, call: _Call, ids: tuple[str, ...]) -> dict[str, VideoMeta]:
        body = await self._data(
            call, VIDEOS_LIST_URL, "videos.list", part="snippet,contentDetails", id=",".join(ids)
        )
        return parse_videos(body)

    async def _published_count(
        self, call: _Call, first: date, last: date
    ) -> tuple[int | None, bool]:
        body = await self._data(
            call, CHANNELS_LIST_URL, "channels.list", part="contentDetails", mine="true"
        )
        playlist = parse_uploads_playlist(body)
        if playlist is None:
            call.note("uploads_playlist_not_found")
            return None, False
        count = 0
        token: str | None = None
        for _page in range(MAX_PLAYLIST_PAGES):
            params = {"part": "contentDetails", "playlistId": playlist, "maxResults": "50"}
            if token:
                params["pageToken"] = token
            dates, token = parse_playlist_page(
                await self._data(call, PLAYLIST_ITEMS_URL, "playlistItems.list", **params)
            )
            count += sum(1 for d in dates if first <= d <= last)
            if token is None:
                return count, False
            if dates and all(d < first for d in dates):
                call.note("uploads_playlist_order_assumed_newest_first")
                return count, False
        call.note("published_count_truncated")
        return count, True

    async def _request(self, url: str, params: Mapping[str, str], scope: str) -> Any:
        """GET して JSON を返す。失敗は ``_Stop``（状態）に写す。401 は更新して 1 回だけ再試行。"""
        try:
            response = await self._send(url, params)
            if response.status_code == 401:
                self._credentials.invalidate()
                response = await self._send(url, params)
        except YouTubeAuthError:
            raise _Stop(ToolStatus.PERMISSION_DENIED, "oauth_refresh_rejected") from None
        except YouTubeError:
            raise _Stop(ToolStatus.FAILED, "oauth_unavailable") from None
        except httpx.TimeoutException:
            raise _Stop(ToolStatus.TIMEOUT, "request_timeout") from None
        except httpx.HTTPError:
            raise _Stop(ToolStatus.FAILED, "transport_error") from None
        code = response.status_code
        if code == 200:
            return response.json()
        if code == 401:
            raise _Stop(ToolStatus.PERMISSION_DENIED, "unauthorized")
        if code == 429:
            raise _Stop(ToolStatus.RATE_LIMITED, "http_429")
        if code == 403:
            reason = _reason(response)
            if reason in _QUOTA_REASONS:
                raise _Stop(ToolStatus.QUOTA_EXCEEDED, reason)
            if reason in _RATE_REASONS:
                raise _Stop(ToolStatus.RATE_LIMITED, reason)
            raise _Stop(ToolStatus.PERMISSION_DENIED, await self._forbidden_code(scope, reason))
        if code == 400:
            raise _Stop(ToolStatus.FAILED, "invalid_query")
        raise _Stop(ToolStatus.FAILED, f"http_{code}")

    async def _forbidden_code(self, scope: str, reason: str | None) -> str:
        try:
            granted = await self.granted_scopes()
        except AnalyticsUnavailableError:
            return "forbidden_scope_unverified"
        if scope not in granted:
            return "scope_missing"
        return f"forbidden:{reason}" if reason else "forbidden"

    async def _send(self, url: str, params: Mapping[str, str]) -> httpx.Response:
        token = await self._credentials.access_token()
        return await self._client.get(
            url, params=dict(params), headers={"Authorization": f"Bearer {token}"}
        )


def _reason(response: httpx.Response) -> str | None:
    try:
        errors = response.json()["error"]["errors"]
        reason = errors[0]["reason"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    return (
        reason if isinstance(reason, str) and reason.isidentifier() and len(reason) <= 40 else None
    )


def _aggregate(meta: VideoMeta, rows: list[dict[str, Any]]) -> tuple[str, CompareVideo]:
    views_by_type: dict[str, float] = {}
    for r in rows:
        k = str(r["creatorContentType"])
        views_by_type[k] = views_by_type.get(k, 0.0) + (_num(r.get("views")) or 0.0)
    kind = max(sorted(views_by_type), key=lambda k: views_by_type[k])
    mine = [r for r in rows if str(r["creatorContentType"]) == kind]

    def total(metric: str) -> float:
        return sum(_num(r.get(metric)) or 0.0 for r in mine)

    def weighted(metric: str) -> float | None:
        return weighted_mean(
            (v, w)
            for r in mine
            if (v := _num(r.get(metric))) is not None and (w := _num(r.get("views"))) is not None
        )

    video = CompareVideo(
        video_id=meta.video_id,
        published_at=meta.published_at,
        duration_seconds=meta.duration_seconds,
        views=total("views"),
        engaged_views=total("engagedViews"),
        estimated_minutes_watched=total("estimatedMinutesWatched"),
        average_view_duration_seconds=weighted("averageViewDuration"),
        average_view_percentage=weighted("averageViewPercentage"),
    )
    return kind, video


_RANKED = (
    "views",
    "engaged_views",
    "estimated_minutes_watched",
    "average_view_duration_seconds",
    "average_view_percentage",
)


def _group(kind: str, entries: list[tuple[CompareVideo, date]], n: int) -> CompareGroup:
    videos = [v for v, _ in entries]
    views_ok = not (kind == "SHORTS" and any(shorts_views_affected(s) for _, s in entries))
    if not views_ok:
        videos = [v.model_copy(update={"views": None}) for v in videos]
    if len(videos) < 2:
        return CompareGroup(
            content_type=kind,
            status="insufficient_data",
            reason="fewer_than_2_videos_in_group",
            window_days=n,
            views_status="ok" if views_ok else "insufficient_data",
            videos=tuple(videos),
        )
    ranking: dict[str, tuple[str, ...]] = {}
    for metric in _RANKED:
        if metric == "views" and not views_ok:
            continue
        have = [(getattr(v, metric), v.video_id) for v in videos if getattr(v, metric) is not None]
        have.sort(key=lambda t: (-t[0], t[1]))
        ranking[metric] = tuple(vid for _, vid in have)
    return CompareGroup(
        content_type=kind,
        status="ok",
        window_days=n,
        views_status="ok" if views_ok else "insufficient_data",
        videos=tuple(videos),
        ranking_within_group=ranking,
    )
