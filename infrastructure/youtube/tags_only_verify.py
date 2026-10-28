"""タグだけの照合が、description のマーカー照合と同じ動画を見つけるかの確認（ADR-0075、INV-110）。

**読み取り専用。** 本番で ``AVP_UPLOAD_DESCRIPTION_WITHOUT_MARKER`` を ON にする前に、所有者が
読み取り専用の OAuth で実行し、既存の private 動画すべてで一致することを確かめるための部品。

- 呼ぶ API は GET の ``channels.list`` / ``playlistItems.list`` / ``videos.list`` だけ
- 実行前に access token の scope を確かめ、``youtube.readonly`` 以外（書き込み系を含む）が1つでも
  付いていれば何も呼ばずに拒否する
- このモジュールは投稿 adapter（``uploader``）を import しない。insert / update / delete を持たない
- 出力は件数と video id だけ。title・description・tags・token は出さない

判定は ``contracts.upload`` の ``marker_in_tags`` / ``marker_in_description_lines``
（adapter と同じ定義）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from contracts.upload import (
    UPLOAD_MARKER_PATTERN,
    marker_in_description_lines,
    marker_in_tags,
)
from infrastructure.youtube.errors import YouTubeAuthError, YouTubeTransientError
from infrastructure.youtube.oauth import TOKENINFO_ENDPOINT, RefreshTokenCredentials

API_BASE = "https://www.googleapis.com/youtube/v3"
READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
#: 許す scope はこれだけ。書き込み系・Analytics・未知の scope は拒否する
ALLOWED_SCOPES = frozenset({READONLY_SCOPE})
PAGE_SIZE = 50
DEFAULT_MAX_PAGES = 40

_MARKER_LINE = re.compile(UPLOAD_MARKER_PATTERN)


class WriteScopeRefusedError(Exception):
    """token に ``youtube.readonly`` 以外の scope が付いている。何も呼ばずに止める。"""


@dataclass(frozen=True)
class VideoSample:
    """照合に必要な最小の情報。出力には載せない（video id 以外）。"""

    video_id: str
    privacy: str
    tags: tuple[str, ...] = ()
    description: str = ""


@dataclass
class VerificationReport:
    """件数と video id だけ（title / description / tags は持たない）。"""

    scanned: int = 0
    non_private_skipped: int = 0
    without_marker: int = 0
    #: タグにだけマーカーがある（新方式で投稿された）動画。比較の対象外
    tags_marker_only: int = 0
    with_description_marker: int = 0
    agree: int = 0
    disagree_ids: list[str] = field(default_factory=list)
    #: 一覧を最後まで読めなかった（ページ上限）。読めていない範囲があるので結論にしない
    truncated: bool = False

    @property
    def conclusive(self) -> bool:
        """比較対象があり、一覧を読み切っているときだけ結論として扱う。"""
        return self.with_description_marker > 0 and not self.truncated

    @property
    def full_agreement(self) -> bool:
        return self.conclusive and not self.disagree_ids

    def lines(self) -> list[str]:
        percent = (
            f"{100 * self.agree / self.with_description_marker:.1f}%"
            if self.with_description_marker
            else "n/a"
        )
        verdict = (
            "AGREE (100%)"
            if self.full_agreement
            else "DISAGREE"
            if self.disagree_ids
            else "INCONCLUSIVE"
        )
        out = [
            f"private videos scanned: {self.scanned}",
            f"non-private skipped: {self.non_private_skipped}",
            f"no avpu marker anywhere: {self.without_marker}",
            f"marker in tags only (not compared): {self.tags_marker_only}",
            f"with description marker: {self.with_description_marker}",
            f"tags-only finds the same video: {self.agree} ({percent})",
            f"list truncated: {'yes' if self.truncated else 'no'}",
            f"result: {verdict}",
        ]
        out.extend(f"mismatch video id: {video_id}" for video_id in self.disagree_ids)
        return out


def _description_markers(description: str) -> list[str]:
    return [
        line.strip() for line in description.splitlines() if _MARKER_LINE.fullmatch(line.strip())
    ]


def _first(videos: Sequence[VideoSample], hit: Any) -> str | None:
    """一覧の順（新しい順）で最初に一致した video id。adapter の走査順と同じ。"""
    for video in videos:
        if hit(video):
            return video.video_id
    return None


def verify(videos: Sequence[VideoSample], *, truncated: bool = False) -> VerificationReport:
    """private 動画の一覧（新しい順）から、2つの照合の一致を数える。純粋関数（I/O なし）。

    description にマーカー行を持つ動画ごとに、そのマーカーで (a) description 照合 (b) タグだけの照合
    を一覧に対して行い、同じ video id を返すかを見る。タグが無い・別の動画のタグにある、は不一致。
    """
    report = VerificationReport(truncated=truncated)
    private = [v for v in videos if v.privacy == "private"]
    report.non_private_skipped = len(videos) - len(private)
    report.scanned = len(private)
    for video in private:
        markers = _description_markers(video.description)
        if not markers:
            if any(_MARKER_LINE.fullmatch(tag) for tag in video.tags):
                report.tags_marker_only += 1
            else:
                report.without_marker += 1
            continue
        report.with_description_marker += 1
        agreed = all(
            _first(private, lambda v, m=marker: marker_in_description_lines(m, v.description))
            == _first(private, lambda v, m=marker: marker_in_tags(m, v.tags))
            for marker in markers
        )
        if agreed:
            report.agree += 1
        else:
            report.disagree_ids.append(video.video_id)
    return report


# ----------------------------------------------------------------------- 読み取り専用の取得


class ReadOnlyYouTube:
    """GET だけの client。scope を確かめてから一覧を読む。"""

    def __init__(
        self,
        credentials: RefreshTokenCredentials,
        *,
        client: httpx.AsyncClient,
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        self._credentials = credentials
        self._client = client
        self._max_pages = max_pages

    async def granted_scopes(self) -> frozenset[str]:
        """token に付いた scope（tokeninfo。token は URL でなく form で送る）。"""
        token = await self._credentials.access_token()
        response = await self._client.post(TOKENINFO_ENDPOINT, data={"access_token": token})
        if response.status_code != 200:
            raise YouTubeTransientError(f"tokeninfo failed: HTTP {response.status_code}")
        try:
            scope = response.json().get("scope", "")
        except (ValueError, AttributeError):
            raise YouTubeTransientError("tokeninfo response is malformed") from None
        if not isinstance(scope, str):
            raise YouTubeTransientError("tokeninfo response is malformed")
        return frozenset(scope.split())

    async def assert_read_only(self) -> None:
        scopes = await self.granted_scopes()
        if not scopes or not scopes <= ALLOWED_SCOPES:
            # scope の名前は出さない（数だけ）
            raise WriteScopeRefusedError(
                f"the token carries {len(scopes - ALLOWED_SCOPES)} scope(s) other than "
                f"youtube.readonly, or no readable scope ({len(scopes)} total); "
                "use a read-only token"
            )

    async def _get(self, resource: str, params: Mapping[str, str]) -> dict[str, Any]:
        token = await self._credentials.access_token()
        try:
            response = await self._client.get(
                f"{API_BASE}/{resource}",
                params=dict(params),
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.HTTPError as exc:
            raise YouTubeTransientError(
                f"{resource}: transport failure ({type(exc).__name__})"
            ) from None
        if response.status_code in (401, 403):
            raise YouTubeAuthError(f"{resource}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise YouTubeTransientError(f"{resource}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise YouTubeTransientError(f"{resource}: response is not JSON") from None
        if not isinstance(body, dict):
            raise YouTubeTransientError(f"{resource}: response is not an object")
        return body

    async def list_videos(self) -> tuple[list[VideoSample], bool]:
        """uploads playlist を新しい順に読む。``(videos, truncated)``。"""
        channels = await self._get("channels", {"part": "contentDetails", "mine": "true"})
        try:
            playlist_id = channels["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except (KeyError, IndexError, TypeError):
            raise YouTubeAuthError("no channel for these credentials") from None
        videos: list[VideoSample] = []
        page_token: str | None = None
        for _ in range(self._max_pages):
            params = {
                "part": "contentDetails",
                "playlistId": str(playlist_id),
                "maxResults": str(PAGE_SIZE),
            }
            if page_token:
                params["pageToken"] = page_token
            page = await self._get("playlistItems", params)
            items = page.get("items")
            if not isinstance(items, list):
                raise YouTubeTransientError("playlistItems: malformed page")
            ids = [
                i["contentDetails"]["videoId"]
                for i in items
                if isinstance(i, dict)
                and isinstance(i.get("contentDetails"), dict)
                and isinstance(i["contentDetails"].get("videoId"), str)
            ]
            if ids:
                body = await self._get("videos", {"part": "snippet,status", "id": ",".join(ids)})
                found = body.get("items")
                if not isinstance(found, list):
                    raise YouTubeTransientError("videos: malformed response")
                by_id = {v.get("id"): v for v in found if isinstance(v, dict)}
                for video_id in ids:  # playlist の順（新しい順）を保つ
                    videos.append(_sample(video_id, by_id.get(video_id)))
            next_token = page.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                return videos, False
            page_token = next_token
        return videos, True


def _sample(video_id: str, video: Mapping[str, Any] | None) -> VideoSample:
    if video is None:
        # 一覧に出たのに videos.list が返さない（削除・非公開化）。private としては数えない
        return VideoSample(video_id=video_id, privacy="unavailable")
    snippet = video.get("snippet") if isinstance(video.get("snippet"), dict) else {}
    status = video.get("status") if isinstance(video.get("status"), dict) else {}
    tags = snippet.get("tags")
    description = snippet.get("description")
    return VideoSample(
        video_id=video_id,
        privacy=str(status.get("privacyStatus", "")),
        tags=tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else (),
        description=description if isinstance(description, str) else "",
    )


async def run_live(
    credentials: RefreshTokenCredentials, *, client: httpx.AsyncClient, max_pages: int
) -> VerificationReport:
    api = ReadOnlyYouTube(credentials, client=client, max_pages=max_pages)
    await api.assert_read_only()
    videos, truncated = await api.list_videos()
    return verify(videos, truncated=truncated)


__all__ = [
    "ALLOWED_SCOPES",
    "READONLY_SCOPE",
    "ReadOnlyYouTube",
    "VerificationReport",
    "VideoSample",
    "WriteScopeRefusedError",
    "run_live",
    "verify",
]
