"""probe_final_video: 固定版 ffmpeg で作った小さな mp4 を実際にデコードする。

バイナリが無い環境（scripts/install-render-ffmpeg.sh 未実行）では skip する。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from domain.errors import MediaValidationError
from domain.render.ports import FinalVideoProbe
from infrastructure.media.probe import PillowAvMediaProbe

FFMPEG = Path(
    os.environ.get("RENDER_FFMPEG_PATH", str(Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"))
)
needs_ffmpeg = pytest.mark.skipif(not FFMPEG.is_file(), reason="static ffmpeg not installed")


def _encode(out: Path, *, audio: bool) -> None:
    argv = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=108x192:rate=30:duration=1",
    ]
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=1"]
        argv += ["-ac", "2", "-c:a", "aac", "-b:a", "96k"]
    argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", "-y", str(out)]
    subprocess.run(argv, check=True, timeout=60)


def test_probe_satisfies_the_port() -> None:
    assert isinstance(PillowAvMediaProbe(), FinalVideoProbe)


@needs_ffmpeg
def test_h264_aac(tmp_path: Path) -> None:
    out = tmp_path / "final.mp4"
    _encode(out, audio=True)
    info = PillowAvMediaProbe().probe_final_video(str(out))
    assert (info.width, info.height, info.fps_millis) == (108, 192, 30_000)
    assert info.frames_decoded == 30 and info.decode_errors == 0
    assert abs(info.duration_ms - 1000) <= 40
    assert (info.video_codec, info.pix_fmt) == ("h264", "yuv420p")
    assert info.audio_present and info.audio_codec == "aac"
    assert (info.audio_sample_rate_hz, info.audio_channels) == (48_000, 2)
    assert info.audio_duration_ms is not None and abs(info.audio_duration_ms - 1000) <= 60
    assert info.bytes == out.stat().st_size > 0


@needs_ffmpeg
def test_video_without_audio(tmp_path: Path) -> None:
    out = tmp_path / "silent.mp4"
    _encode(out, audio=False)
    info = PillowAvMediaProbe().probe_final_video(str(out))
    assert info.frames_decoded == 30
    assert not info.audio_present
    assert info.audio_codec is None and info.audio_sample_rate_hz is None
    assert info.audio_channels is None and info.audio_duration_ms is None


def test_garbage_and_missing_files_are_validation_errors(tmp_path: Path) -> None:
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video" * 100)
    with pytest.raises(MediaValidationError):
        PillowAvMediaProbe().probe_final_video(str(bad))
    with pytest.raises(MediaValidationError):
        PillowAvMediaProbe().probe_final_video(str(tmp_path / "missing.mp4"))
