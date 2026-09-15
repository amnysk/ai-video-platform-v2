"""動画アップロード先のポート（Phase 6）。

実装（YouTube resumable upload）は ``infrastructure/youtube``、テストは
``tests/support/fake_youtube.py``。ここは HTTP も OAuth も知らない。

session URI は短命な **アップロード権限**（capability）なので ``repr`` に出さない。
保存先は予約台帳の evidence だけ（ログ・Artifact・API 応答に出さない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from contracts.upload import UPLOAD_CONTENT_TYPE


@dataclass(frozen=True, slots=True)
class UploadSessionRef:
    """開始済み resumable session への不透明な参照。"""

    uri: str = field(repr=False)
    total_bytes: int
    #: session 開始時の ``X-Upload-Content-Type``。media PUT の ``Content-Type`` にも同じ値を送る
    content_type: str = UPLOAD_CONTENT_TYPE

    def __repr__(self) -> str:
        return (
            f"UploadSessionRef(uri=<redacted>, total_bytes={self.total_bytes}, "
            f"content_type={self.content_type})"
        )


@dataclass(frozen=True, slots=True)
class UploadIncomplete:
    """未完了。次に送るべき先頭バイト位置（= 受理済みバイト数）。"""

    next_offset: int


@dataclass(frozen=True, slots=True)
class UploadCompleted:
    """全バイト受理。動画 resource が作られた。"""

    video_id: str


@dataclass(frozen=True, slots=True)
class UploadExpired:
    """session が失効した。バイトが送られていたかは呼び出し側の記録で判断する。"""


type UploadProgress = UploadIncomplete | UploadCompleted | UploadExpired


class VideoUploader(Protocol):
    """resumable upload の最小操作。すべて非同期で、キャンセルされても状態を壊さない。

    - ``start_session`` は動画を作らない（何度呼んでも重複投稿にならない）。
    - ``send_chunk`` / ``query_status`` は同じ session に対して再試行してよい。
    - 失敗は adapter 固有の例外（auth / quota / rate limit / rejected / transient）で返す。
    """

    async def start_session(
        self, metadata_json: Mapping[str, Any], total_bytes: int, content_type: str
    ) -> UploadSessionRef: ...

    async def query_status(self, session: UploadSessionRef) -> UploadProgress: ...

    async def send_chunk(
        self, session: UploadSessionRef, offset: int, chunk: bytes, total_bytes: int
    ) -> UploadProgress: ...

    async def find_video_by_marker(self, marker_tag: str) -> str | None: ...
