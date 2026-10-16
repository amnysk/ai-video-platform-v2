"""プロセスをまたいで状態が残る ``FakeVideoUploader``（YouTube の代わり。ネットワークに出ない）。

各操作の前にファイルから状態を読み、後に書く（アトミックな置き換え）。worker を SIGKILL しても
「YouTube 側」の session・受理済みバイト・動画・カウンタは残る。同時に動く worker は1つだけの前提。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import pickle
from pathlib import Path
from typing import Any

from tests.support.fake_youtube import FakeVideoUploader

_FIELDS = (
    "sessions",
    "videos",
    "sessions_started",
    "chunk_sends",
    "status_queries",
    "marker_lookups",
    "processing_checks",
)


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return pickle.loads(path.read_bytes())  # noqa: S301 - テストが書いたファイル


class FileBackedFakeUploader(FakeVideoUploader):
    def __init__(self, state_file: Path, *, chunk_delay: float = 0.0, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._state_file = state_file
        self._chunk_delay = chunk_delay

    def _load(self) -> None:
        state = read_state(self._state_file)
        for name in _FIELDS:
            if name in state:
                setattr(self, name, state[name])
        self._ids = itertools.count(state.get("next_id", 1))

    def _save(self) -> None:
        next_id = next(self._ids)
        self._ids = itertools.count(next_id)
        state = {name: getattr(self, name) for name in _FIELDS} | {"next_id": next_id}
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_bytes(pickle.dumps(state))
        os.replace(tmp, self._state_file)

    async def _around(self, op: Any, *args: Any) -> Any:
        self._load()
        try:
            return await op(*args)
        finally:
            self._save()

    async def start_session(self, metadata_json, total_bytes, content_type):  # type: ignore[override]
        return await self._around(super().start_session, metadata_json, total_bytes, content_type)

    async def query_status(self, session):  # type: ignore[override]
        return await self._around(super().query_status, session)

    async def send_chunk(self, session, offset, chunk, total_bytes):  # type: ignore[override]
        if self._chunk_delay:
            await asyncio.sleep(self._chunk_delay)
        return await self._around(super().send_chunk, session, offset, chunk, total_bytes)

    async def find_video_by_marker(self, marker_tag):  # type: ignore[override]
        return await self._around(super().find_video_by_marker, marker_tag)

    async def processing_status(self, video_id):  # type: ignore[override]
        return await self._around(super().processing_status, video_id)
