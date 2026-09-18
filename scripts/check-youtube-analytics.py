#!/usr/bin/env python3
"""YouTube Analytics の疎通確認（手動。ADR-0025）。

    .venv/bin/python scripts/check-youtube-analytics.py
    DATABASE_URL=... .venv/bin/python scripts/check-youtube-analytics.py --save-snapshot

出すのは channel id・疎通 OK/NG と理由の分類・窓ごとの動画行数・snapshot を保存できるか、だけ。
client secret・refresh token・access token・指標の値は出さない（INV-20）。
既定では DB に書かない（payload を作って JSON 直列化と往復を検査する dry-run）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

# ``python scripts/check-youtube-analytics.py`` で repo のパッケージを import できるように
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from collections.abc import Callable, Set  # noqa: E402
from dataclasses import dataclass, field  # noqa: E402
from datetime import date  # noqa: E402

import httpx  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from domain.errors import AnalyticsUnavailableError  # noqa: E402
from domain.topic_planning import AnalyticsReport  # noqa: E402
from infrastructure.analytics.youtube_analytics import (  # noqa: E402
    PROVIDER_ID,
    AnalyticsScopeMissingError,
    YouTubeAnalyticsProvider,
)
from infrastructure.config import Settings  # noqa: E402
from infrastructure.db.repositories import AnalyticsSnapshotRepository  # noqa: E402
from infrastructure.db.session import build_session_factory  # noqa: E402
from infrastructure.youtube.errors import YouTubeAuthError  # noqa: E402
from infrastructure.youtube.oauth import RefreshTokenCredentials  # noqa: E402
from workers.planning.topic_activities import report_from_payload, report_to_payload  # noqa: E402

DEFAULT_WINDOWS: frozenset[int] = frozenset({7, 28, 90})

#: NG の理由の分類（値や URL は持たない）
NOT_CONFIGURED = "not_configured"
TOKEN_FILE = "token_file_unusable"
SCOPE_MISSING = "scope_missing"
UNAVAILABLE = "api_unavailable"


@dataclass
class CheckResult:
    channel_id: str | None
    ok: bool = False
    reason: str | None = None
    hint: str | None = None
    rows_by_window: dict[int, int] = field(default_factory=dict)
    snapshot: str = "not checked"

    def lines(self) -> list[str]:
        out = [
            f"channel id: {self.channel_id or '(YOUTUBE_CHANNEL_ID unset)'}",
            "api connectivity: OK" if self.ok else f"api connectivity: NG ({self.reason})",
        ]
        if self.hint:
            out.append(f"hint: {self.hint}")
        for window, n in sorted(self.rows_by_window.items()):
            out.append(f"video metric rows ({window}d): {n}")
        out.append(f"snapshot: {self.snapshot}")
        return out


def build_provider(
    settings: Settings, client: httpx.AsyncClient
) -> YouTubeAnalyticsProvider | CheckResult:
    """組めなければ理由入りの ``CheckResult``。"""
    result = CheckResult(channel_id=settings.youtube_channel_id)
    if not (
        settings.youtube_client_id
        and settings.youtube_client_secret
        and settings.youtube_refresh_token_path
    ):
        result.reason = NOT_CONFIGURED
        result.hint = "set YOUTUBE_CLIENT_ID / YOUTUBE_CLIENT_SECRET / YOUTUBE_REFRESH_TOKEN_PATH"
        return result
    try:
        credentials = RefreshTokenCredentials.from_file(
            settings.youtube_client_id,
            settings.youtube_client_secret,
            settings.youtube_refresh_token_path,
            client=client,
        )
    except YouTubeAuthError as exc:
        result.reason = TOKEN_FILE
        result.hint = str(exc)  # adapter が token を含めないよう作っている
        return result
    return YouTubeAnalyticsProvider(credentials, client=client)


def validate_payload(payload: dict[str, object]) -> None:
    """snapshot に保存できる形か（JSON にでき、往復で同じ report に戻る）。"""
    encoded = json.loads(json.dumps(payload, allow_nan=False))
    if report_to_payload(report_from_payload(encoded)) != encoded:
        raise ValueError("snapshot payload does not round-trip")


async def run_check(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    windows: Set[int] = DEFAULT_WINDOWS,
    today: Callable[[], date] = date.today,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    provider: YouTubeAnalyticsProvider | None = None,
) -> CheckResult:
    """``session_factory`` を渡したときだけ snapshot を保存する。"""
    built = provider if provider is not None else build_provider(settings, client)
    if isinstance(built, CheckResult):
        return built
    result = CheckResult(channel_id=settings.youtube_channel_id)
    try:
        await built.verify_scopes()
        report: AnalyticsReport = await built.fetch(windows)
    except AnalyticsScopeMissingError:
        result.reason = SCOPE_MISSING
        result.hint = "scope missing: yt-analytics.readonly; re-run scripts/youtube-oauth.py"
        return result
    except AnalyticsUnavailableError as exc:
        result.reason = f"{UNAVAILABLE}: {type(exc).__name__}"
        return result
    result.ok = True
    result.rows_by_window = {w: len(v) for w, v in report.videos_by_window.items()}

    payload = report_to_payload(report)
    try:
        validate_payload(payload)
    except (TypeError, ValueError) as exc:
        result.snapshot = f"NG ({type(exc).__name__})"
        return result
    if session_factory is None:
        result.snapshot = "OK (dry-run; not saved)"
        return result
    async with session_factory() as session:
        saved = await AnalyticsSnapshotRepository(session).save(today(), PROVIDER_ID, payload)
        await session.commit()
    result.snapshot = f"saved ({saved.snapshot_date.isoformat()}, provider={PROVIDER_ID})"
    return result


HTTP_TIMEOUT_SECONDS = 30.0


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check YouTube Analytics access (no secrets)")
    parser.add_argument(
        "--save-snapshot",
        action="store_true",
        help="save today's snapshot (needs DATABASE_URL set explicitly in the environment)",
    )
    args = parser.parse_args(argv)
    settings = Settings()
    session_factory = None
    if args.save_snapshot:
        if not os.environ.get("DATABASE_URL"):
            print("--save-snapshot needs DATABASE_URL in the environment", file=sys.stderr)
            return 2
        session_factory = build_session_factory(os.environ["DATABASE_URL"])
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        result = await run_check(settings, client, session_factory=session_factory)
    for line in result.lines():
        print(line)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
