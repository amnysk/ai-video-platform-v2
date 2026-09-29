"""``scripts/check-youtube-analytics.py``（ADR-0025）。MockTransport と SQLite だけ（INV-18）。

秘密（client secret・refresh token・access token）を出力に出さないことも確かめる。
"""

from __future__ import annotations

import pathlib
from datetime import date
from typing import Any

import httpx
import pytest

from domain.topic_planning import AnalyticsReport, AudienceShares, VideoMetrics
from infrastructure.analytics.youtube_analytics import (
    ANALYTICS_SCOPE,
    PROVIDER_ID,
    UPLOAD_SCOPES,
    VIDEO_METRICS,
)
from infrastructure.config import Settings
from infrastructure.db.repositories import AnalyticsSnapshotRepository
from infrastructure.youtube.oauth import TOKEN_ENDPOINT, TOKENINFO_ENDPOINT
from tests.support.script_loader import load_script_module
from workers.planning.topic_activities import report_from_payload, report_to_payload

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts/check-youtube-analytics.py"
CLIENT_SECRET = "client-secret-value-xyz"
REFRESH = "1//refresh-token-value-xyz"
ACCESS = "ya29.access-token-value-xyz"
SECRETS = (CLIENT_SECRET, REFRESH, ACCESS)
TODAY = date(2026, 9, 19)

# scripts/ に .pyc を残さない。ロード中のガードだけでなく、ロード後に
# sys.modules へ残さないことも重要（tests/support/script_loader.py 参照）。
check = load_script_module("check_youtube_analytics", SCRIPT)


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path, **env: str) -> Settings:
    for key in (
        "YOUTUBE_CLIENT_ID",
        "YOUTUBE_CLIENT_SECRET",
        "YOUTUBE_REFRESH_TOKEN_PATH",
        "YOUTUBE_CHANNEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)  # pyright: ignore[reportCallIssue]


def _configured(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> Settings:
    token = tmp_path / "refresh-token"
    token.write_text(REFRESH, encoding="utf-8")
    token.chmod(0o600)
    return _settings(
        monkeypatch,
        tmp_path,
        YOUTUBE_CLIENT_ID="client-id",
        YOUTUBE_CLIENT_SECRET=CLIENT_SECRET,
        YOUTUBE_REFRESH_TOKEN_PATH=str(token),
        YOUTUBE_CHANNEL_ID="UCchannel123",
    )


def _handler(*, scopes: str, ages: bool = True, country: bool = True):
    def handle(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if url == TOKENINFO_ENDPOINT:
            return httpx.Response(200, json={"scope": scopes})
        dims = req.url.params.get("dimensions")
        if dims == "video":
            span = date.fromisoformat(req.url.params["endDate"]) - date.fromisoformat(
                req.url.params["startDate"]
            )
            n = {6: 1, 27: 2}.get(span.days, 3)
            rows = [[f"v{i}", 10.0, 1, 2, 50.0, 1, 0, 0, 0] for i in range(n)]
            headers = ["video", *VIDEO_METRICS]
        elif dims == "ageGroup" and ages:
            headers, rows = ["ageGroup", "viewerPercentage"], [["age18-24", 100.0]]
        elif dims == "country" and country:
            headers, rows = ["country", "views"], [["US", 5]]
        else:
            return httpx.Response(400)
        return httpx.Response(
            200, json={"columnHeaders": [{"name": h} for h in headers], "rows": rows}
        )

    return handle


ALL = " ".join([*UPLOAD_SCOPES, ANALYTICS_SCOPE])


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _no_secrets(lines: list[str]) -> None:
    text = "\n".join(lines)
    for secret in SECRETS:
        assert secret not in text


@pytest.mark.asyncio
async def test_ok_dry_run_prints_only_counts(monkeypatch, tmp_path) -> None:
    settings = _configured(monkeypatch, tmp_path)
    async with _client(_handler(scopes=ALL)) as client:
        result = await check.run_check(settings, client, today=lambda: TODAY)
    assert result.ok
    assert result.rows_by_window == {7: 1, 28: 2, 90: 3}
    lines = result.lines()
    assert lines[0] == "channel id: UCchannel123"
    assert "api connectivity: OK" in lines
    assert "video metric rows (90d): 3" in lines
    assert "snapshot: OK (dry-run; not saved)" in lines
    _no_secrets(lines)


@pytest.mark.asyncio
async def test_age_and_geography_unavailable_is_still_ok(monkeypatch, tmp_path) -> None:
    settings = _configured(monkeypatch, tmp_path)
    async with _client(_handler(scopes=ALL, ages=False, country=False)) as client:
        result = await check.run_check(settings, client)
    assert result.ok
    assert result.rows_by_window[28] == 2


@pytest.mark.asyncio
async def test_missing_analytics_scope_is_reported_clearly(monkeypatch, tmp_path) -> None:
    settings = _configured(monkeypatch, tmp_path)
    async with _client(_handler(scopes=" ".join(UPLOAD_SCOPES))) as client:
        result = await check.run_check(settings, client)
    assert not result.ok
    assert result.reason == check.SCOPE_MISSING
    text = "\n".join(result.lines())
    assert "scope missing" in text and "scripts/youtube-oauth.py" in text
    _no_secrets(result.lines())


@pytest.mark.asyncio
async def test_not_configured(monkeypatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    async with _client(_handler(scopes=ALL)) as client:
        result = await check.run_check(settings, client)
    assert not result.ok and result.reason == check.NOT_CONFIGURED
    assert "(YOUTUBE_CHANNEL_ID unset)" in result.lines()[0]


@pytest.mark.asyncio
async def test_unreadable_token_file(monkeypatch, tmp_path) -> None:
    settings = _configured(monkeypatch, tmp_path)
    pathlib.Path(str(settings.youtube_refresh_token_path)).chmod(0o644)
    async with _client(_handler(scopes=ALL)) as client:
        result = await check.run_check(settings, client)
    assert not result.ok and result.reason == check.TOKEN_FILE
    _no_secrets(result.lines())


@pytest.mark.asyncio
async def test_api_down_is_ng_with_reason_class(monkeypatch, tmp_path) -> None:
    settings = _configured(monkeypatch, tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        if str(req.url) == TOKEN_ENDPOINT:
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        return httpx.Response(503)

    async with _client(handler) as client:
        result = await check.run_check(settings, client)
    assert not result.ok
    assert result.reason is not None and result.reason.startswith(check.UNAVAILABLE)
    _no_secrets(result.lines())


@pytest.mark.asyncio
async def test_save_snapshot_roundtrips_through_sqlite(
    monkeypatch, tmp_path, session_factory
) -> None:
    settings = _configured(monkeypatch, tmp_path)
    async with _client(_handler(scopes=ALL)) as client:
        result = await check.run_check(
            settings, client, today=lambda: TODAY, session_factory=session_factory
        )
    assert result.ok and result.snapshot.startswith("saved (2026-09-19")
    async with session_factory() as session:
        saved = await AnalyticsSnapshotRepository(session).latest(PROVIDER_ID)
    assert saved is not None
    report = report_from_payload(saved.payload)
    assert {w: len(v) for w, v in report.videos_by_window.items()} == {7: 1, 28: 2, 90: 3}
    assert report.audience.age_groups == {"age18-24": 1.0}
    assert report.audience.genders is None


def test_payload_roundtrip_with_new_and_old_fields() -> None:
    report = AnalyticsReport(
        videos_by_window={28: [VideoMetrics("v", 1, 2, 3, 4, 5, 6, 7, subscribers_lost=1)]},
        audience=AudienceShares(country_us=0.5, genders={"female": 0.4, "male": 0.6}),
    )
    payload = report_to_payload(report)
    check.validate_payload(payload)
    assert report_from_payload(payload) == report
    # 追加前の snapshot（subscribers_lost・構成比の dict が無い）も読める
    old = {
        "videos_by_window": {
            "7": [
                {
                    "video_id": "v",
                    "views": 1,
                    "estimated_minutes_watched": 2,
                    "average_view_duration": 3,
                    "average_view_percentage": 4,
                    "likes": 5,
                    "shares": 6,
                    "subscribers_gained": 7,
                }
            ]
        },
        "audience": {"country_us": 0.5, "age_18_24": None, "age_25_34": None, "shorts": None},
    }
    legacy = report_from_payload(old)
    assert legacy.videos_by_window[7][0].subscribers_lost == 0.0
    assert legacy.audience.genders is None


@pytest.mark.asyncio
async def test_save_snapshot_requires_explicit_database_url(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert await check._main(["--save-snapshot"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
