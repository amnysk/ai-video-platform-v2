"""取得した音源の検査（ADR-0087 §3）。ffprobe / ffmpeg を子プロセスで呼ぶ（ネットワークには触れない）。

- 音声ストリームがあり、映像（アルバムアート以外）が無い
- 尺が範囲内（``ACQUISITION_MIN/MAX_DURATION_MS``）
- 全体をデコードしてエラーが無い（``-xerror``）
- ebur128 で integrated loudness と true peak を実測する。無音は不合格
- テンポの推定は任意（今は ``None``）。推定はライセンスの事実と混ぜない
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from contracts.music_sources import ACQUISITION_MAX_DURATION_MS, ACQUISITION_MIN_DURATION_MS


class AudioInvalidError(Exception):
    """音源として使えない（形式・尺・デコード・無音）。``reason`` は固定語彙。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AudioReport:
    container: str
    codec: str
    duration_ms: int
    sample_rate: int
    channels: int
    integrated_lufs: float
    true_peak_dbtp: float
    tempo_bpm: int | None = None

    def as_analysis(self) -> dict[str, Any]:
        """``bgm_tracks.analysis``（実測。客観値だけ）。"""
        return asdict(self)


_I = re.compile(r"Integrated loudness:\s*\n\s*I:\s*(-?(?:inf|[\d.]+))\s*LUFS")
_TP = re.compile(r"True peak:\s*\n\s*Peak:\s*(-?(?:inf|[\d.]+))\s*dBFS")
_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO = re.compile(r"Audio:\s*(\w+).*?(\d+) Hz,\s*(\w+)")


def _num(text: str) -> float:
    return float(text)


class FfmpegAudioInspector:
    def __init__(
        self,
        ffmpeg_path: str | Path,
        *,
        ffprobe_path: str | Path | None = None,
        min_duration_ms: int = ACQUISITION_MIN_DURATION_MS,
        max_duration_ms: int = ACQUISITION_MAX_DURATION_MS,
        timeout_seconds: int = 300,
    ) -> None:
        self._ffmpeg = str(ffmpeg_path)
        sibling = Path(self._ffmpeg).with_name("ffprobe")
        if ffprobe_path is not None:
            self._ffprobe: str | None = str(ffprobe_path)
        else:
            self._ffprobe = str(sibling) if sibling.is_file() else None
        self._min = min_duration_ms
        self._max = max_duration_ms
        self._timeout = timeout_seconds

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603
                argv, capture_output=True, text=True, timeout=self._timeout, check=False,
                stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except subprocess.TimeoutExpired as exc:
            raise AudioInvalidError("inspect_timeout") from exc

    def _probe(self, path: Path) -> tuple[str, str, int, int, int]:
        """(container, codec, duration_ms, sample_rate, channels)。"""
        if self._ffprobe is not None:
            proc = self._run(
                [self._ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]
            )  # fmt: skip
            if proc.returncode != 0:
                raise AudioInvalidError("not_media", proc.stderr.strip()[:200])
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise AudioInvalidError("not_media") from exc
            streams = data.get("streams") or []
            audio = [s for s in streams if s.get("codec_type") == "audio"]
            video = [
                s for s in streams
                if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
            ]  # fmt: skip
            if not audio:
                raise AudioInvalidError("no_audio_stream")
            if video:
                raise AudioInvalidError("has_video_stream")
            fmt = data.get("format") or {}
            try:
                duration_ms = round(float(fmt.get("duration") or audio[0].get("duration")) * 1000)
                return (
                    str(fmt.get("format_name") or ""),
                    str(audio[0].get("codec_name") or ""),
                    duration_ms,
                    int(audio[0].get("sample_rate") or 0),
                    int(audio[0].get("channels") or 0),
                )
            except (TypeError, ValueError) as exc:
                raise AudioInvalidError("no_duration") from exc
        proc = self._run([self._ffmpeg, "-nostdin", "-hide_banner", "-i", str(path)])
        text = proc.stderr
        dur = _DURATION.search(text)
        aud = _AUDIO.search(text)
        if dur is None or aud is None or "Video:" in text.replace("Video: mjpeg", "").replace("Video: png", ""):
            raise AudioInvalidError("not_audio")
        h, m, s = dur.groups()
        duration_ms = round((int(h) * 3600 + int(m) * 60 + float(s)) * 1000)
        return "unknown", aud.group(1), duration_ms, int(aud.group(2)), 2 if aud.group(3) == "stereo" else 1

    def inspect(self, path: Path) -> AudioReport:
        container, codec, duration_ms, rate, channels = self._probe(path)
        if duration_ms < self._min:
            raise AudioInvalidError("too_short", f"{duration_ms}ms")
        if duration_ms > self._max:
            raise AudioInvalidError("too_long", f"{duration_ms}ms")
        proc = self._run(
            [self._ffmpeg, "-nostdin", "-hide_banner", "-xerror", "-v", "info", "-i", str(path),
             "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]
        )  # fmt: skip
        if proc.returncode != 0:
            raise AudioInvalidError("decode_failed", proc.stderr.strip()[-200:])
        i = _I.search(proc.stderr)
        tp = _TP.search(proc.stderr)
        if i is None or tp is None:
            raise AudioInvalidError("loudness_unavailable")
        if "inf" in i.group(1) or "inf" in tp.group(1):
            raise AudioInvalidError("silent")
        return AudioReport(
            container=container,
            codec=codec,
            duration_ms=duration_ms,
            sample_rate=rate,
            channels=channels,
            integrated_lufs=_num(i.group(1)),
            true_peak_dbtp=_num(tp.group(1)),
            tempo_bpm=None,
        )


__all__ = ["AudioInvalidError", "AudioReport", "FfmpegAudioInspector"]
