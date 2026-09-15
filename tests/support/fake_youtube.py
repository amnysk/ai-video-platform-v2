"""インメモリの YouTube（``VideoUploader`` の fake）。実ネットワークに出ない。

失敗注入で「応答だけ失われた」「途中で失効」「認証・quota」を再現し、
``videos_created`` で重複投稿が起きていないことを検査する。
"""

from __future__ import annotations

import asyncio
import itertools
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from contracts.upload import DEFAULT_UPLOAD_CHUNK_BYTES
from domain.upload.ports import (
    UploadCompleted,
    UploadExpired,
    UploadIncomplete,
    UploadProgress,
    UploadSessionRef,
    VideoProcessingState,
)
from infrastructure.youtube.errors import YouTubeTransientError

FAKE_SESSION_PREFIX = "fake-youtube://session/"


@dataclass
class FakeSession:
    total_bytes: int
    content_type: str
    metadata: dict[str, Any]
    data: bytearray = field(default_factory=bytearray)
    video_id: str | None = None
    expired: bool = False


@dataclass
class FakeVideo:
    video_id: str
    metadata: dict[str, Any]
    data: bytes

    @property
    def tags(self) -> list[str]:
        snippet = self.metadata.get("snippet") or {}
        return list(snippet.get("tags") or [])

    @property
    def description_lines(self) -> list[str]:
        snippet = self.metadata.get("snippet") or {}
        return [line.strip() for line in str(snippet.get("description") or "").splitlines()]


class FakeVideoUploader:
    """失敗注入つきの ``VideoUploader``。フィールドを書き換えて注入する。"""

    #: 認証中アカウントのチャンネル（``own_channel_id``）。tests/support/upload.CHANNEL_ID と同じ
    DEFAULT_CHANNEL_ID = "UC" + "a" * 22

    def __init__(
        self, *, chunk_bytes: int = DEFAULT_UPLOAD_CHUNK_BYTES, channel_id: str = DEFAULT_CHANNEL_ID
    ) -> None:
        self.channel_id = channel_id
        self.channel_lookups = 0
        #: 最後以外のチャンクはこの長さちょうど（実 adapter と同じ規則。テストは小さくしてよい）
        if chunk_bytes <= 0:
            raise ValueError("chunk_bytes must be positive")
        self.chunk_bytes = chunk_bytes
        self.content_types: list[str] = []
        self.sessions: dict[str, FakeSession] = {}
        self.videos: dict[str, FakeVideo] = {}
        self.sessions_started = 0
        self.chunk_sends = 0
        self.status_queries = 0
        self.marker_lookups = 0
        #: 次の N 回の send_chunk を、バイトを受理せず transient で失敗させる
        self.fail_chunk_sends = 0
        #: 完了（動画作成）直後の応答を失い transient を投げる回数
        self.lose_completion_responses = 0
        #: 受理済みバイトがこの値以上になったら session を失効させる
        self.expire_session_at_offset: int | None = None
        #: 各操作の直前に投げる例外（auth / quota 等）。一度投げたら消える
        self.fail_start_with: Exception | None = None
        self.fail_send_with: Exception | None = None
        self.fail_query_with: Exception | None = None
        self.fail_lookup_with: Exception | None = None
        self.fail_processing_with: Exception | None = None
        #: ``processing_status`` の呼び出し回数
        self.processing_checks = 0
        #: 台本どおりに返す処理状態（全動画共通）。最後の1つは消費せず返し続ける。
        #: 空なら既定（投稿した動画は processed / メタデータの privacy / 自チャンネル）
        self._processing_script: deque[VideoProcessingState] = deque()
        self._ids = itertools.count(1)
        self._lock = asyncio.Lock()

    @property
    def videos_created(self) -> int:
        return len(self.videos)

    def add_existing_video(self, tags: list[str], description: str = "") -> str:
        """チャンネルに既にある動画（照合用）を置く。"""
        video_id = f"vid{next(self._ids):08d}"
        snippet = {"tags": list(tags), "description": description}
        self.videos[video_id] = FakeVideo(video_id, {"snippet": snippet}, b"")
        return video_id

    def processing_state(self, **overrides: Any) -> VideoProcessingState:
        """自チャンネル・private・found の状態を土台に一部だけ変えた状態を作る。"""
        base = VideoProcessingState(
            found=True,
            upload_status="processed",
            processing_status="succeeded",
            privacy_status="private",
            channel_id=self.channel_id,
        )
        return replace(base, **overrides)

    def script_processing(self, *states: VideoProcessingState) -> None:
        self._processing_script = deque(states)

    async def processing_status(self, video_id: str) -> VideoProcessingState:
        async with self._lock:
            self.processing_checks += 1
            exc, self.fail_processing_with = self.fail_processing_with, None
            self._take(exc)
            if self._processing_script:
                if len(self._processing_script) > 1:
                    return self._processing_script.popleft()
                return self._processing_script[0]
            video = self.videos.get(video_id)
            if video is None:
                return VideoProcessingState(found=False)
            status = video.metadata.get("status") or {}
            return self.processing_state(privacy_status=status.get("privacyStatus", "private"))

    async def own_channel_id(self) -> str:
        async with self._lock:
            self.channel_lookups += 1
            return self.channel_id

    def expire_all_sessions(self) -> None:
        for session in self.sessions.values():
            session.expired = True

    def _session(self, ref: UploadSessionRef) -> FakeSession | None:
        session = self.sessions.get(ref.uri)
        if session is None or session.expired:
            return None
        return session

    @staticmethod
    def _take(exc: Exception | None) -> None:
        if exc is not None:
            raise exc

    async def start_session(
        self, metadata_json: Mapping[str, Any], total_bytes: int, content_type: str
    ) -> UploadSessionRef:
        async with self._lock:
            exc, self.fail_start_with = self.fail_start_with, None
            self._take(exc)
            if total_bytes <= 0:
                raise ValueError("total_bytes must be positive")
            self.sessions_started += 1
            uri = f"{FAKE_SESSION_PREFIX}{next(self._ids)}"
            self.sessions[uri] = FakeSession(total_bytes, content_type, dict(metadata_json))
            return UploadSessionRef(uri=uri, total_bytes=total_bytes, content_type=content_type)

    def _progress(self, session: FakeSession) -> UploadProgress:
        if session.video_id is not None:
            return UploadCompleted(session.video_id)
        return UploadIncomplete(len(session.data))

    async def query_status(self, session: UploadSessionRef) -> UploadProgress:
        async with self._lock:
            self.status_queries += 1
            exc, self.fail_query_with = self.fail_query_with, None
            self._take(exc)
            state = self._session(session)
            if state is None:
                return UploadExpired()
            return self._progress(state)

    async def send_chunk(
        self, session: UploadSessionRef, offset: int, chunk: bytes, total_bytes: int
    ) -> UploadProgress:
        async with self._lock:
            self.chunk_sends += 1
            exc, self.fail_send_with = self.fail_send_with, None
            self._take(exc)
            state = self._session(session)
            if state is None:
                return UploadExpired()
            if total_bytes != state.total_bytes or offset + len(chunk) > total_bytes:
                raise ValueError("chunk range is outside the upload")
            if not chunk or (offset + len(chunk) < total_bytes and len(chunk) != self.chunk_bytes):
                raise ValueError("non-final chunk must be exactly chunk_bytes")
            self.content_types.append(session.content_type)
            if self.fail_chunk_sends > 0:
                self.fail_chunk_sends -= 1
                raise YouTubeTransientError("send upload chunk: HTTP 503 (injected)")
            if state.video_id is not None:
                return UploadCompleted(state.video_id)
            if offset != len(state.data):
                # 実 API と同じく、受理済み位置と合わない送信は現状を返すだけ
                return self._progress(state)
            state.data.extend(chunk)
            limit = self.expire_session_at_offset
            if limit is not None and len(state.data) >= limit and len(state.data) < total_bytes:
                state.expired = True
                raise YouTubeTransientError("send upload chunk: transport failure (injected)")
            if len(state.data) == total_bytes:
                video_id = f"vid{next(self._ids):08d}"
                state.video_id = video_id
                self.videos[video_id] = FakeVideo(video_id, state.metadata, bytes(state.data))
                if self.lose_completion_responses > 0:
                    self.lose_completion_responses -= 1
                    raise YouTubeTransientError("send upload chunk: response lost (injected)")
            return self._progress(state)

    async def find_video_by_marker(self, marker_tag: str) -> str | None:
        async with self._lock:
            self.marker_lookups += 1
            exc, self.fail_lookup_with = self.fail_lookup_with, None
            self._take(exc)
            for video in reversed(list(self.videos.values())):
                if marker_tag in video.tags or marker_tag in video.description_lines:
                    return video.video_id
            return None
