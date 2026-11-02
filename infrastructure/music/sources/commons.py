"""Wikimedia Commons（MediaWiki API）のクライアント（ADR-0087 §1 / §3）。

- API は ``https://commons.wikimedia.org/w/api.php`` と、応答の原本 URL
  （``upload.wikimedia.org``）だけ。**HTML のスクレイピングはしない**
- Wikimedia の方針: 説明的な User-Agent（連絡先つき）・直列・``maxlag``・タイムアウト
  （docs/research/bgm-sources-2026-11.md）
- ダウンロードは streaming でバイト数の上限を超えたら打ち切り、Commons が報告する sha1 と照合する
- redirect は追わない・環境の proxy 設定は使わない
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from contracts.music_sources import (
    ACQUISITION_ALLOWED_EXTENSIONS,
    ACQUISITION_HTTP_TIMEOUT_SECONDS,
    ACQUISITION_MAX_BYTES_PER_FILE,
)
from domain.music.commons_facts import pages_from_response
from infrastructure.music.sources.errors import CandidateInvalidError, SourceUnavailableError

COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"
ORIGINAL_FILE_HOST = "upload.wikimedia.org"
IMAGEINFO_BATCH = 50
MAXLAG_SECONDS = 5
MAXLAG_RETRY_CAP_SECONDS = 30.0
_IIPROP = "url|size|mime|sha1|extmetadata|mediatype|timestamp"


def validate_user_agent(user_agent: str) -> str:
    """Wikimedia の方針: クライアント名 + 連絡先。ブラウザ・汎用 UA は不可。"""
    ua = user_agent.strip()
    lowered = ua.lower()
    if (
        not ua
        or "(" not in ua
        or ")" not in ua
        or not ("@" in ua or "http" in lowered)
        or lowered.startswith(("python-", "curl", "mozilla"))
    ):
        raise ValueError(
            "User-Agent must look like '<client>/<ver> (<contact: email or url>)' "
            "(Wikimedia User-Agent policy)"
        )
    return ua


@dataclass(frozen=True, slots=True)
class DownloadedFile:
    data: bytes
    sha256: str
    sha1: str
    extension: str


def extension_of(url: str) -> str:
    path = urlsplit(url).path
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class CommonsClient:
    def __init__(
        self,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        api_url: str = COMMONS_API_URL,
        timeout_seconds: float = ACQUISITION_HTTP_TIMEOUT_SECONDS,
        max_bytes_per_file: int = ACQUISITION_MAX_BYTES_PER_FILE,
        allowed_extensions: Sequence[str] = ACQUISITION_ALLOWED_EXTENSIONS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        request_gap_seconds: float = 1.0,
    ) -> None:
        self._ua = validate_user_agent(user_agent)
        self._api_url = api_url
        self._max_bytes = max_bytes_per_file
        self._allowed = frozenset(allowed_extensions)
        self._sleep = sleep
        self._gap = request_gap_seconds
        self._lock = asyncio.Lock()  # 直列（並列にしない）
        self._first = True
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout_seconds),
            headers={"User-Agent": self._ua, "Accept-Encoding": "gzip"},
            follow_redirects=False,
            trust_env=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _pace(self) -> None:
        if not self._first and self._gap > 0:
            await self._sleep(self._gap)
        self._first = False

    async def _api(self, params: Mapping[str, str | int]) -> dict[str, Any]:
        query = {"format": "json", "formatversion": 2, "maxlag": MAXLAG_SECONDS, **params}
        async with self._lock:
            for attempt in (1, 2):
                await self._pace()
                try:
                    response = await self._client.get(self._api_url, params=query)
                except httpx.HTTPError as exc:
                    raise SourceUnavailableError(f"commons api: {type(exc).__name__}") from exc
                if response.status_code >= 400:
                    raise SourceUnavailableError(f"commons api: HTTP {response.status_code}")
                try:
                    body = response.json()
                except ValueError as exc:
                    raise SourceUnavailableError("commons api: invalid JSON") from exc
                if not isinstance(body, dict):
                    raise SourceUnavailableError("commons api: unexpected JSON")
                error = body.get("error")
                if error is None:
                    return body
                code = error.get("code") if isinstance(error, dict) else None
                if code == "maxlag" and attempt == 1:
                    try:
                        wait = float(response.headers.get("Retry-After", MAXLAG_SECONDS))
                    except ValueError:
                        wait = float(MAXLAG_SECONDS)
                    await self._sleep(min(max(wait, 0.0), MAXLAG_RETRY_CAP_SECONDS))
                    continue
                raise SourceUnavailableError(f"commons api error: {code}")
        raise SourceUnavailableError("commons api: unreachable")  # pragma: no cover

    async def search_titles(self, query: str, *, limit: int = 10) -> list[str]:
        body = await self._api(
            {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srnamespace": 6,
                "srlimit": max(1, min(limit, 50)),
            }
        )
        hits = (body.get("query") or {}).get("search") or []
        return [h["title"] for h in hits if isinstance(h, dict) and isinstance(h.get("title"), str)]

    async def imageinfo(self, titles: Sequence[str]) -> list[dict[str, Any]]:
        """タイトル順のページ（imageinfo / categories / templates つき）。50 件ずつ直列。"""
        pages: list[dict[str, Any]] = []
        for start in range(0, len(titles), IMAGEINFO_BATCH):
            chunk = list(titles[start : start + IMAGEINFO_BATCH])
            body = await self._api(
                {
                    "action": "query",
                    "titles": "|".join(chunk),
                    "prop": "imageinfo|categories|templates",
                    "iiprop": _IIPROP,
                    "cllimit": "max",
                    "tllimit": "max",
                    "tlnamespace": 10,
                }
            )
            got = {str(p.get("title")): p for p in pages_from_response(body)}
            normalized = {
                n["from"]: n["to"]
                for n in (body.get("query") or {}).get("normalized") or []
                if isinstance(n, dict) and "from" in n and "to" in n
            }
            for title in chunk:
                page = got.get(normalized.get(title, title))
                if page is not None:
                    pages.append(page)
        return pages

    async def download(self, url: str, *, expected_sha1: str) -> DownloadedFile:
        parts = urlsplit(url)
        if parts.scheme != "https" or parts.hostname != ORIGINAL_FILE_HOST:
            raise CandidateInvalidError("host_not_allowed", parts.hostname or "")
        ext = extension_of(url)
        if ext not in self._allowed:
            raise CandidateInvalidError("extension_not_allowed", ext)
        async with self._lock:
            await self._pace()
            sha1 = hashlib.sha1(usedforsecurity=False)  # noqa: S324 - Commons の報告値との照合
            sha256 = hashlib.sha256()
            chunks: list[bytes] = []
            total = 0
            try:
                async with self._client.stream("GET", url) as response:
                    if 300 <= response.status_code < 400:
                        raise CandidateInvalidError("redirect_not_followed", str(response.status_code))
                    if response.status_code in (404, 410):
                        raise CandidateInvalidError("gone", str(response.status_code))
                    if response.status_code >= 400:
                        raise SourceUnavailableError(f"download: HTTP {response.status_code}")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
                        raise CandidateInvalidError("too_large", declared)
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_bytes:
                            raise CandidateInvalidError("too_large", f">{self._max_bytes}")
                        sha1.update(chunk)
                        sha256.update(chunk)
                        chunks.append(chunk)
            except httpx.HTTPError as exc:
                raise SourceUnavailableError(f"download: {type(exc).__name__}") from exc
        if sha1.hexdigest() != expected_sha1.lower():
            raise CandidateInvalidError("sha1_mismatch")
        if total == 0:
            raise CandidateInvalidError("empty")
        return DownloadedFile(b"".join(chunks), sha256.hexdigest(), sha1.hexdigest(), ext)


__all__ = [
    "COMMONS_API_URL",
    "CommonsClient",
    "DownloadedFile",
    "extension_of",
    "validate_user_agent",
]
