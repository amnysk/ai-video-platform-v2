"""地図 PNG を render profile のシーン動画（mp4）にする（ADR-0084 §4）。

argv は ``infrastructure.media.map_clip.build_map_clip_argv``（純粋）。実行はこの 1 箇所だけ。
render worker が起動時に sha256 を検証した固定版 ffmpeg を使う。ネットワークに出ない。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from contracts.render import RenderProfile
from domain.errors import RenderEngineFailedError
from infrastructure.media.map_clip import build_map_clip_argv

#: クリップ符号化の版。argv の組み方を変えたら上げる（描画 identity に入り再描画される）
MAP_CLIP_VERSION = "1"
CLIP_TIMEOUT_SECONDS = 120.0


@runtime_checkable
class MapClipEncoder(Protocol):
    """同期。失敗は ``RenderEngineFailedError``（再試行可能）。"""

    def encode(self, png: Path, output: Path, duration_ms: int, profile: RenderProfile) -> None: ...


class FfmpegMapClipEncoder:
    def __init__(self, ffmpeg_path: str | Path, timeout_seconds: float = CLIP_TIMEOUT_SECONDS):
        self._ffmpeg = str(ffmpeg_path)
        self._timeout = timeout_seconds

    def encode(self, png: Path, output: Path, duration_ms: int, profile: RenderProfile) -> None:
        argv = build_map_clip_argv(self._ffmpeg, png.name, output.name, duration_ms, profile)
        try:
            done = subprocess.run(  # noqa: S603 - argv は純粋関数で組んだ固定形
                argv, cwd=png.parent, capture_output=True, timeout=self._timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderEngineFailedError(
                f"map clip encoding failed: {type(exc).__name__}"
            ) from exc
        if done.returncode != 0 or not (png.parent / output.name).is_file():
            tail = done.stderr.decode("utf-8", "replace")[-300:]
            raise RenderEngineFailedError(f"map clip encoding failed rc={done.returncode}: {tail}")


__all__ = ["CLIP_TIMEOUT_SECONDS", "MAP_CLIP_VERSION", "FfmpegMapClipEncoder", "MapClipEncoder"]
