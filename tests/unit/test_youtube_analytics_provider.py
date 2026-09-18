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
    ANALYTICS_SCOPE,
    REPORTS_ENDPOINT,
    SCOPE_MISSING_MESSAGE,
    UPLOAD_SCOPES,
    VIDEO_METRICS,
    AnalyticsScopeMissingError,
    YouTubeAnalyticsProvider,
)
from infrastructure.youtube.oauth import (
    TOKEN_ENDPOINT,
    TOKENINFO_ENDPOINT,
    RefreshTokenCredentials,
)

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
    return [[f"vid{i}", views * (i + 1), 10, 30, 75.5, 3, 1, 0, 2] for i in range(n)]


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
    if dims == "gender":
        return _report(["gender", "viewerPercentage"], [["male", 70.0], ["female", 30.0]])
    if dims == "creatorContentType":
        return _report(["creatorContentType", "views"], [["SHORTS", 90], ["VIDEO_ON_DEMAND", 10]])
    return httpx.Response(400)


ALL_SCOPES = " ".join([*UPLOAD_SCOPES, ANALYTICS_SCOPE])


class Recorder:
    def __init__(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        scopes: str | None = ALL_SCOPES,
    ) -> None:
        self.handler = handler
        self.scopes = scopes
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.tokeninfo_bodies: list[bytes] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if str(req.url) == TOKEN_ENDPOINT:
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if str(req.url) == TOKENINFO_ENDPOINT:
            self.tokeninfo_bodies.append(req.content)
            if self.scopes is None:
                return httpx.Response(400, json={"error": "invalid_token"})
            return httpx.Response(200, json={"scope": self.scopes, "expires_in": 3500})
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
    assert report.audience.genders == pytest.approx({"male": 0.7, "female": 0.3})
    assert report.audience.age_groups is not None
    assert report.audience.age_groups["age35-44"] == pytest.approx(0.45)
    assert report.audience.countries == pytest.approx({"US": 0.6, "JP": 0.4})
    assert report.audience.content_types == pytest.approx({"SHORTS": 0.9, "VIDEO_ON_DEMAND": 0.1})
    assert vids[0].subscribers_lost == 2.0

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


@pytest.mark.asyncio
async def test_audience_dimensions_are_independent_and_tolerate_403() -> None:
    """年齢・国が取れなくても（403 / 400）、性別・Shorts と動画指標は残る。"""

    def handler(req: httpx.Request) -> httpx.Response:
        dims = req.url.params.get("dimensions")
        if dims == "ageGroup":
            return httpx.Response(403)
        if dims == "country":
            return httpx.Response(400)
        return default_handler(req)

    report = await provider(Recorder(handler)).fetch({7, 28, 90})
    assert {w: len(v) for w, v in report.videos_by_window.items()} == {7: 3, 28: 3, 90: 3}
    a = report.audience
    assert a.age_18_24 is None and a.age_groups is None
    assert a.country_us is None and a.countries is None
    assert a.genders == pytest.approx({"male": 0.7, "female": 0.3})
    assert a.shorts == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_countries_keep_top_ten() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("dimensions") == "country":
            return _report(["country", "views"], [[f"C{i:02d}", 100 - i] for i in range(15)])
        return default_handler(req)

    report = await provider(Recorder(handler)).fetch({28})
    assert report.audience.countries is not None
    assert list(report.audience.countries) == [f"C{i:02d}" for i in range(10)]
    assert report.audience.country_us == 0.0


@pytest.mark.asyncio
async def test_403_without_analytics_scope_says_rerun_consent() -> None:
    rec = Recorder(lambda _r: httpx.Response(403), scopes=" ".join(UPLOAD_SCOPES))
    with pytest.raises(AnalyticsScopeMissingError) as info:
        await provider(rec).fetch({28})
    assert str(info.value) == SCOPE_MISSING_MESSAGE
    assert "scripts/youtube-oauth.py" in str(info.value)
    assert isinstance(info.value, AnalyticsUnavailableError)
    # token は URL に載せず form で送る
    assert rec.tokeninfo_bodies == [b"access_token=tok"]


@pytest.mark.asyncio
async def test_403_with_scope_granted_stays_generic() -> None:
    rec = Recorder(lambda _r: httpx.Response(403))
    with pytest.raises(AnalyticsUnavailableError) as info:
        await provider(rec).fetch({28})
    assert not isinstance(info.value, AnalyticsScopeMissingError)


@pytest.mark.asyncio
async def test_403_when_tokeninfo_fails_stays_generic() -> None:
    rec = Recorder(lambda _r: httpx.Response(403), scopes=None)
    with pytest.raises(AnalyticsUnavailableError) as info:
        await provider(rec).fetch({28})
    assert not isinstance(info.value, AnalyticsScopeMissingError)


@pytest.mark.asyncio
async def test_verify_scopes() -> None:
    ok = await provider(Recorder(default_handler)).verify_scopes()
    assert ANALYTICS_SCOPE in ok and set(UPLOAD_SCOPES) <= ok
    with pytest.raises(AnalyticsScopeMissingError):
        await provider(Recorder(default_handler, scopes=" ".join(UPLOAD_SCOPES))).verify_scopes()
    with pytest.raises(AnalyticsUnavailableError):
        await provider(Recorder(default_handler, scopes=None)).verify_scopes()


def test_repr_is_redacted() -> None:
    assert "refresh" not in repr(provider(Recorder(default_handler)))


def test_empty_audience_breakdown_is_unknown_not_zero() -> None:
    """行が1件も無い = データが無い。0% と取り違えない（レビュー指摘）。"""
    from infrastructure.analytics.youtube_analytics import _pick

    assert _pick({}, "US") is None
    assert _pick(None, "US") is None
    # 他の国の行があって US が無いなら US 視聴は 0
    assert _pick({"JP": 1.0}, "US") == 0.0
