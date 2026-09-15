"""YouTube Data API v3 resumable upload（httpx のみ、Phase 6）。

プロトコル（developers.google.com/youtube/v3/guides/using_resumable_upload_protocol）:

1. ``POST /upload/youtube/v3/videos?uploadType=resumable&part=snippet,status``
   （``X-Upload-Content-Length`` / ``X-Upload-Content-Type``、JSON body）→ ``Location`` = session。
   session の開始は動画を作らない。
2. ``PUT <session>``（``Content-Range: bytes a-b/total``）→ 308（``Range: bytes=0-N``、無ければ 0）
   または 200/201（動画 resource）。
3. 状態確認は ``PUT <session>``（空 body、``Content-Range: bytes */total``）。404/410 = 失効。

API に冪等キーは無いので、結果不明時の照合は uploads playlist 上の marker tag で行う。
session URI（upload_id）はアップロード権限なので、例外文・ログに出さない。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx

from contracts.upload import DEFAULT_UPLOAD_CHUNK_BYTES, YOUTUBE_CHUNK_ALIGNMENT_BYTES
from domain.upload.ports import (
    UploadCompleted,
    UploadExpired,
    UploadIncomplete,
    UploadProgress,
    UploadSessionRef,
    VideoProcessingState,
)
from infrastructure.youtube.errors import (
    YouTubeAuthError,
    YouTubeQuotaError,
    YouTubeRateLimitError,
    YouTubeRejectedError,
    YouTubeTransientError,
)
from infrastructure.youtube.oauth import RefreshTokenCredentials

logger = logging.getLogger(__name__)

UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
API_BASE_URL = "https://www.googleapis.com/youtube/v3"
SESSION_HOST = "www.googleapis.com"
SESSION_PATH = "/upload/youtube/v3/videos"

CHUNK_UNIT_BYTES = YOUTUBE_CHUNK_ALIGNMENT_BYTES
DEFAULT_CHUNK_BYTES = DEFAULT_UPLOAD_CHUNK_BYTES
MAX_PAGE_SIZE = 50
DEFAULT_LOOKUP_PAGES = 5
REQUIRED_PRIVACY = "private"

QUOTA_REASONS = frozenset({"quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"})
RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
AUTH_REASONS = frozenset(
    {
        "forbidden",
        "youtubeSignupRequired",
        "authorizationRequired",
        "invalid_grant",
        "insufficientPermissions",
        "channelNotFound",
    }
)
REJECTED_REASONS = frozenset(
    {
        "invalidTitle",
        "invalidDescription",
        "invalidTags",
        "invalidCategoryId",
        "invalidVideoMetadata",
        "forbiddenPrivacySetting",
        "mediaBodyRequired",
        # videos.insert の公式エラー（2026-09-15）。403 でも入力の拒否であり認証ではない
        "forbiddenLicenseSetting",
        "invalidFilename",
        "defaultLanguageNotSet",
        "invalidRecordingDetails",
        "invalidPublishAt",
        "invalidVideoGameRating",
    }
)

#: 処理状態の値・video id として受け入れる文字（token や URL を状態に持ち込まない）
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_RANGE_RE = re.compile(r"^bytes=0-(\d+)$")
_SECRETISH_RE = re.compile(r"(upload_id|access_token|refresh_token|code)=[^&\s\"']+")


def redact(text: str) -> str:
    """URL の query に載る権限・token を伏せる。"""
    return _SECRETISH_RE.sub(r"\1=<redacted>", text)


def validate_chunk_bytes(chunk_bytes: int) -> int:
    if chunk_bytes <= 0 or chunk_bytes % CHUNK_UNIT_BYTES:
        raise ValueError(f"chunk size must be a positive multiple of 256 KiB, got {chunk_bytes}")
    return chunk_bytes


def _error_reasons(response: httpx.Response) -> list[str]:
    try:
        body = response.json()
    except ValueError:
        return []
    reasons: list[str] = []
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str):
            reasons.append(error)
        elif isinstance(error, dict):
            for item in error.get("errors") or []:
                if isinstance(item, dict) and isinstance(item.get("reason"), str):
                    reasons.append(item["reason"])
            for detail in error.get("details") or []:
                if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                    reasons.append(detail["reason"])
    return [r for r in reasons if r.isidentifier()]


def _raise_for_error(response: httpx.Response, operation: str) -> None:
    """2xx/308 以外を分類して投げる（404/410 と 401 は呼び出し側で先に扱う）。"""
    status = response.status_code
    reasons = _error_reasons(response)
    label = f"{operation}: HTTP {status}" + (f" ({','.join(reasons)})" if reasons else "")
    reason_set = set(reasons)
    if reason_set & QUOTA_REASONS:
        raise YouTubeQuotaError(label)
    if reason_set & RATE_LIMIT_REASONS or status == 429:
        raise YouTubeRateLimitError(label)
    if reason_set & REJECTED_REASONS:
        raise YouTubeRejectedError(label)
    if reason_set & AUTH_REASONS or status in (401, 403):
        raise YouTubeAuthError(label)
    if status >= 500:
        raise YouTubeTransientError(label)
    if 400 <= status < 500:
        raise YouTubeRejectedError(label)
    raise YouTubeTransientError(label)


def _safe(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_VALUE_RE.match(value) else None


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _check_session_uri(uri: str) -> None:
    parsed = urlparse(uri)
    if parsed.scheme != "https" or parsed.hostname != SESSION_HOST or parsed.path != SESSION_PATH:
        raise YouTubeTransientError("upload session uri has an unexpected origin")


class YouTubeResumableUploader:
    """``domain.upload.ports.VideoUploader`` の YouTube 実装。"""

    def __init__(
        self,
        credentials: RefreshTokenCredentials,
        *,
        client: httpx.AsyncClient,
        chunk_bytes: int = DEFAULT_CHUNK_BYTES,
        max_lookup_pages: int = DEFAULT_LOOKUP_PAGES,
    ) -> None:
        self._credentials = credentials
        self._client = client
        self.chunk_bytes = validate_chunk_bytes(chunk_bytes)
        if max_lookup_pages < 1:
            raise ValueError("max_lookup_pages must be >= 1")
        self._max_lookup_pages = max_lookup_pages

    def __repr__(self) -> str:
        return f"YouTubeResumableUploader(chunk_bytes={self.chunk_bytes})"

    # --- HTTP ---------------------------------------------------------------

    async def _request(
        self,
        method: str,
        url: str,
        operation: str,
        *,
        headers: Mapping[str, str] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Bearer を付けて送る。401 なら token を1回だけ更新して再送する。"""
        for attempt in (0, 1):
            token = await self._credentials.access_token()
            merged = {**(headers or {}), "Authorization": f"Bearer {token}"}
            try:
                response = await self._client.request(method, url, headers=merged, **kwargs)
            except httpx.TransportError as exc:
                raise YouTubeTransientError(
                    f"{operation}: transport failure ({type(exc).__name__})"
                ) from None
            if response.status_code != 401:
                return response
            self._credentials.invalidate()
            if attempt == 1:
                break
        raise YouTubeAuthError(f"{operation}: HTTP 401 after token refresh")

    # --- VideoUploader --------------------------------------------------------

    async def start_session(
        self, metadata_json: Mapping[str, Any], total_bytes: int, content_type: str
    ) -> UploadSessionRef:
        if total_bytes <= 0:
            raise ValueError("total_bytes must be positive")
        status = metadata_json.get("status")
        privacy = status.get("privacyStatus") if isinstance(status, Mapping) else None
        if privacy != REQUIRED_PRIVACY:
            # INV-19: この段階では非公開以外を送らない（adapter でも二重に止める）
            raise YouTubeRejectedError("upload metadata must set status.privacyStatus=private")
        response = await self._request(
            "POST",
            UPLOAD_URL,
            "start upload session",
            params={
                "uploadType": "resumable",
                "part": "snippet,status",
                "notifySubscribers": "false",
            },
            headers={
                "X-Upload-Content-Length": str(total_bytes),
                "X-Upload-Content-Type": content_type,
            },
            json=dict(metadata_json),
        )
        if response.status_code != 200:
            _raise_for_error(response, "start upload session")
        location = response.headers.get("Location")
        if not location:
            raise YouTubeTransientError("start upload session: response has no Location")
        _check_session_uri(location)
        return UploadSessionRef(uri=location, total_bytes=total_bytes, content_type=content_type)

    async def query_status(self, session: UploadSessionRef) -> UploadProgress:
        _check_session_uri(session.uri)
        response = await self._request(
            "PUT",
            session.uri,
            "query upload status",
            headers={"Content-Range": f"bytes */{session.total_bytes}", "Content-Length": "0"},
        )
        return self._progress(response, "query upload status", session.total_bytes)

    async def send_chunk(
        self, session: UploadSessionRef, offset: int, chunk: bytes, total_bytes: int
    ) -> UploadProgress:
        _check_session_uri(session.uri)
        if total_bytes != session.total_bytes:
            raise ValueError("total_bytes does not match the session")
        end = offset + len(chunk)
        if offset < 0 or not chunk or end > total_bytes:
            raise ValueError("chunk range is outside the upload")
        if end < total_bytes and len(chunk) != self.chunk_bytes:
            # 最後以外のチャンクは設定値ちょうど（256 KiB の倍数）。短いのは最後だけ
            raise ValueError(f"non-final chunk must be exactly chunk_bytes ({self.chunk_bytes})")
        response = await self._request(
            "PUT",
            session.uri,
            "send upload chunk",
            headers={
                "Content-Range": f"bytes {offset}-{end - 1}/{total_bytes}",
                "Content-Type": session.content_type,
            },
            content=chunk,
        )
        return self._progress(response, "send upload chunk", total_bytes)

    @staticmethod
    def _progress(response: httpx.Response, operation: str, total_bytes: int) -> UploadProgress:
        status = response.status_code
        if status in (200, 201):
            try:
                body = response.json()
            except ValueError:
                body = None
            video_id = body.get("id") if isinstance(body, dict) else None
            if not isinstance(video_id, str) or not video_id:
                raise YouTubeTransientError(f"{operation}: completed response has no video id")
            return UploadCompleted(video_id=video_id)
        if status == 308:
            header = response.headers.get("Range")
            if header is None:
                return UploadIncomplete(next_offset=0)
            match = _RANGE_RE.match(header.strip())
            if match is None:
                raise YouTubeTransientError(f"{operation}: unreadable Range header")
            next_offset = int(match.group(1)) + 1
            if next_offset > total_bytes:
                raise YouTubeTransientError(f"{operation}: Range exceeds upload size")
            return UploadIncomplete(next_offset=next_offset)
        if status in (404, 410):
            return UploadExpired()
        _raise_for_error(response, operation)
        raise AssertionError("unreachable")  # pragma: no cover

    async def own_channel_id(self) -> str:
        """認証中のアカウントのチャンネル id（``channels.list(mine=true)``、readonly scope）。"""
        channels = await self._get_json("channels", {"part": "id", "mine": "true"}, "own channel")
        items = channels.get("items") or []
        channel_id = items[0].get("id") if items and isinstance(items[0], dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise YouTubeAuthError("own channel: no channel for these credentials")
        return channel_id

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        """uploads playlist を新しい順に最大 max_lookup_pages × 50 件、マーカーで照合する。

        タグ、または description の行のどれかがマーカーと一致すれば自分の投稿とみなす。
        """
        if not marker_tag:
            raise ValueError("marker_tag must be non-empty")
        channels = await self._get_json(
            "channels", {"part": "contentDetails", "mine": "true"}, "list own channel"
        )
        items = channels.get("items") or []
        try:
            playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except (IndexError, KeyError, TypeError):
            raise YouTubeAuthError("list own channel: no channel for these credentials") from None
        if not isinstance(playlist_id, str) or not playlist_id:
            raise YouTubeTransientError("list own channel: uploads playlist id is unreadable")

        page_token: str | None = None
        for _page in range(self._max_lookup_pages):
            params = {
                "part": "contentDetails",
                "playlistId": playlist_id,
                "maxResults": str(MAX_PAGE_SIZE),
            }
            if page_token:
                params["pageToken"] = page_token
            page = await self._get_json("playlistItems", params, "list uploads")
            video_ids = [
                item["contentDetails"]["videoId"]
                for item in page.get("items") or []
                if isinstance(item, dict)
                and isinstance(item.get("contentDetails"), dict)
                and isinstance(item["contentDetails"].get("videoId"), str)
            ]
            if video_ids:
                videos = await self._get_json(
                    "videos", {"part": "snippet", "id": ",".join(video_ids)}, "list videos"
                )
                for video in videos.get("items") or []:
                    if not isinstance(video, dict):
                        continue
                    snippet = video.get("snippet")
                    tags = snippet.get("tags") if isinstance(snippet, dict) else None
                    description = snippet.get("description") if isinstance(snippet, dict) else None
                    in_tags = isinstance(tags, list) and marker_tag in tags
                    in_description = isinstance(description, str) and marker_tag in (
                        line.strip() for line in description.splitlines()
                    )
                    if in_tags or in_description:
                        video_id = video.get("id")
                        if isinstance(video_id, str) and video_id:
                            return video_id
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return None
            page_token = next_token
        return None

    async def processing_status(self, video_id: str) -> VideoProcessingState:
        """``videos.list(part=status,processingDetails,snippet)``（1 unit、readonly scope）。

        items が空・404 はまだ見えない（``found=False``）。値は識別子として安全なものだけ残す。
        """
        if not _SAFE_VALUE_RE.match(video_id or ""):
            raise ValueError("video_id is not a YouTube video id")
        operation = "video processing status"
        response = await self._request(
            "GET",
            f"{API_BASE_URL}/videos",
            operation,
            params={"part": "status,processingDetails,snippet", "id": video_id},
        )
        if response.status_code == 404:
            return VideoProcessingState(found=False)
        if response.status_code != 200:
            _raise_for_error(response, operation)
        try:
            body = response.json()
        except ValueError:
            raise YouTubeTransientError(f"{operation}: response is not JSON") from None
        items = body.get("items") if isinstance(body, dict) else None
        video = items[0] if isinstance(items, list) and items else None
        if not isinstance(video, dict):
            return VideoProcessingState(found=False)
        status = _mapping(video.get("status"))
        details = _mapping(video.get("processingDetails"))
        snippet = _mapping(video.get("snippet"))
        return VideoProcessingState(
            found=True,
            upload_status=_safe(status.get("uploadStatus")),
            processing_status=_safe(details.get("processingStatus")),
            failure_reason=_safe(status.get("failureReason")),
            rejection_reason=_safe(status.get("rejectionReason")),
            privacy_status=_safe(status.get("privacyStatus")),
            channel_id=_safe(snippet.get("channelId")),
        )

    async def _get_json(
        self, resource: str, params: Mapping[str, str], operation: str
    ) -> dict[str, Any]:
        response = await self._request(
            "GET", f"{API_BASE_URL}/{resource}", operation, params=dict(params)
        )
        if response.status_code != 200:
            _raise_for_error(response, operation)
        try:
            body = response.json()
        except ValueError:
            raise YouTubeTransientError(f"{operation}: response is not JSON") from None
        if not isinstance(body, dict):
            raise YouTubeTransientError(f"{operation}: response is not an object")
        return body
