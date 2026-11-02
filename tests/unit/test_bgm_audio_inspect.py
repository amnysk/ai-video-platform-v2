"""音源の検査（実 ffmpeg。無ければ skip）。合成した曲（第三者の音源を使わない）で確かめる。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from infrastructure.music.audio_inspect import AudioInvalidError, FfmpegAudioInspector
from tests.support.bgm_synth import synthesize

FFMPEG = Path(
    os.environ.get("RENDER_FFMPEG_PATH", str(Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"))
)
pytestmark = pytest.mark.skipif(not FFMPEG.is_file(), reason="static ffmpeg not installed")


@pytest.fixture(scope="module")
def wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("inspect") / "t.wav"
    path.write_bytes(synthesize(32))
    return path


def test_valid_wav_is_measured(wav: Path) -> None:
    r = FfmpegAudioInspector(FFMPEG).inspect(wav)
    assert r.duration_ms == pytest.approx(32_000, abs=50)
    assert r.sample_rate == 24000 and r.channels == 2
    assert -30 < r.integrated_lufs < -5
    assert r.true_peak_dbtp <= 0.5
    assert r.as_analysis()["tempo_bpm"] is None


def test_ffmpeg_only_fallback_reads_the_same_file(wav: Path) -> None:
    # ffprobe を使わない経路（ffprobe が同居しない配置）でも尺と形式を読める
    insp = FfmpegAudioInspector(FFMPEG)
    insp._ffprobe = None  # noqa: SLF001
    r = insp.inspect(wav)
    assert r.duration_ms == pytest.approx(32_000, abs=50) and r.sample_rate == 24000


def test_duration_limits(wav: Path) -> None:
    with pytest.raises(AudioInvalidError) as short:
        FfmpegAudioInspector(FFMPEG, min_duration_ms=60_000).inspect(wav)
    assert short.value.reason == "too_short"
    with pytest.raises(AudioInvalidError) as long:
        FfmpegAudioInspector(FFMPEG, max_duration_ms=10_000).inspect(wav)
    assert long.value.reason == "too_long"


def test_garbage_and_text_are_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "x.flac"
    bad.write_bytes(b"this is not audio" * 100)
    with pytest.raises(AudioInvalidError) as exc:
        FfmpegAudioInspector(FFMPEG).inspect(bad)
    assert exc.value.reason in {
        "not_media",
        "not_audio",
        "no_duration",
        "no_audio_stream",
        "too_short",
    }


def test_truncated_file_fails_to_decode_or_is_short(wav: Path, tmp_path: Path) -> None:
    cut = tmp_path / "cut.wav"
    cut.write_bytes(wav.read_bytes()[:1_000_000])
    # ヘッダの尺と実体が食い違う → デコードの失敗、または尺の不足
    try:
        r = FfmpegAudioInspector(FFMPEG).inspect(cut)
    except AudioInvalidError:
        return
    assert r.duration_ms < 32_000 + 50


def test_silence_is_rejected(tmp_path: Path) -> None:
    silent = tmp_path / "silent.wav"
    subprocess.run(  # noqa: S603
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "anullsrc=r=24000:cl=stereo", "-t", "40", str(silent)],
        check=True, timeout=60,
    )  # fmt: skip
    with pytest.raises(AudioInvalidError) as exc:
        FfmpegAudioInspector(FFMPEG).inspect(silent)
    assert exc.value.reason == "silent"


def test_video_stream_is_rejected(tmp_path: Path) -> None:
    video = tmp_path / "v.mkv"
    subprocess.run(  # noqa: S603
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
         "testsrc=size=64x64:rate=10:duration=40", "-f", "lavfi", "-i",
         "sine=frequency=440:duration=40", "-shortest", str(video)],
        check=True, timeout=120,
    )  # fmt: skip
    with pytest.raises(AudioInvalidError) as exc:
        FfmpegAudioInspector(FFMPEG).inspect(video)
    assert exc.value.reason == "has_video_stream"
