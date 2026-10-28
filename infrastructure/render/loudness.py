"""LoudnessProbe の実装: 固定版 ffmpeg の ebur128 を子プロセスで走らせる（ADR-0077 §5）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from contracts.music_render import LoudnessMeasurement
from domain.errors import RenderEngineFailedError
from domain.render.loudness import LoudnessParseError, parse_ebur128_summary

MEASURE_TIMEOUT_SECONDS = 300


class FfmpegLoudnessProbe:
    def __init__(
        self, ffmpeg: str | Path, *, timeout_seconds: int = MEASURE_TIMEOUT_SECONDS
    ) -> None:
        self._ffmpeg = str(ffmpeg)
        self._timeout = timeout_seconds

    def measure_loudness(self, path: str) -> LoudnessMeasurement:
        argv = [
            self._ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", path, "-vn",
            "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-",
        ]  # fmt: skip
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"},
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RenderEngineFailedError(f"loudness measurement could not run: {exc}") from exc
        if done.returncode != 0:
            raise RenderEngineFailedError(
                f"loudness measurement exited with {done.returncode}: {done.stderr[-500:]}"
            )
        try:
            return parse_ebur128_summary(done.stderr)
        except LoudnessParseError as exc:
            raise RenderEngineFailedError(f"loudness measurement unreadable: {exc}") from exc


__all__ = ["MEASURE_TIMEOUT_SECONDS", "FfmpegLoudnessProbe"]
