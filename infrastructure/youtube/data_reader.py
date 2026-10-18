"""YouTube Data API v3 の**読み取り**だけ（ADR-0047 §5）。Analytics tool の補助。

動画の ``publishedAt`` / 尺 / ``liveBroadcastContent``（``videos.list``、1 unit）と、
投稿数の計数（``channels.list`` → ``playlistItems.list``、各 1 unit。``search.list`` は使わない）。
ここは URL 定数と応答の解釈だけで、通信は呼び出し側が GET で行う（書き込み系の URL は持たない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from infrastructure.youtube.uploader import API_BASE_URL

VIDEOS_LIST_URL = f"{API_BASE_URL}/videos"
CHANNELS_LIST_URL = f"{API_BASE_URL}/channels"
PLAYLIST_ITEMS_URL = f"{API_BASE_URL}/playlistItems"
DATA_API_READ_URLS = frozenset({VIDEOS_LIST_URL, CHANNELS_LIST_URL, PLAYLIST_ITEMS_URL})
DATA_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


@dataclass(frozen=True, slots=True)
class VideoMeta:
    video_id: str
    published_at: str
    published_date: date
    duration_seconds: float | None
    #: ``none`` / ``live`` / ``upcoming``
    live_broadcast_content: str


def parse_duration(value: Any) -> float | None:
    """ISO 8601 の尺 -> 秒。読めなければ None。"""
    if not isinstance(value, str):
        return None
    m = _DURATION.match(value)
    if not m or value in ("P", "PT"):
        return None
    d, h, mi, s = (int(g) if g else 0 for g in m.groups())
    return float(d * 86400 + h * 3600 + mi * 60 + s)


def parse_published(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_videos(body: Any) -> dict[str, VideoMeta]:
    out: dict[str, VideoMeta] = {}
    for item in (body.get("items") or []) if isinstance(body, dict) else []:
        snippet = item.get("snippet") or {}
        published = parse_published(snippet.get("publishedAt"))
        if published is None or not isinstance(item.get("id"), str):
            continue
        out[item["id"]] = VideoMeta(
            video_id=item["id"],
            published_at=published.isoformat(),
            published_date=published.date(),
            duration_seconds=parse_duration((item.get("contentDetails") or {}).get("duration")),
            live_broadcast_content=str(snippet.get("liveBroadcastContent") or "none"),
        )
    return out


def parse_uploads_playlist(body: Any) -> str | None:
    items = (body.get("items") or []) if isinstance(body, dict) else []
    if not items:
        return None
    uploads = ((items[0].get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads")
    return uploads if isinstance(uploads, str) and uploads else None


def parse_playlist_page(body: Any) -> tuple[list[date], str | None]:
    """(各動画の公開日, 次ページ token)。公開日が読めない要素は捨てる。"""
    if not isinstance(body, dict):
        raise ValueError("playlist page is malformed")
    dates: list[date] = []
    for item in body.get("items") or []:
        published = parse_published((item.get("contentDetails") or {}).get("videoPublishedAt"))
        if published is not None:
            dates.append(published.date())
    token = body.get("nextPageToken")
    return dates, token if isinstance(token, str) and token else None
