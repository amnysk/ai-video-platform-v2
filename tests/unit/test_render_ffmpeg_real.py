"""固定版 ffmpeg で実際に描く（バイナリ未導入なら skip）。

小さな合成素材（testsrc の 9:16 無音動画・sine の wav）から、手で組んだ計画を両 profile で描き、
PyAV で測って契約どおりかを確かめる。同じ入力を2回描いてバイト一致（決定性）も確かめる。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
from pathlib import Path

import av
import pytest
from PIL import ImageChops

from domain.render.ports import RenderRequest
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from tests.support.render_plans import make_plan

FFMPEG_SHA256 = "810f94020e76e2b58fb44759a322e86bea5d213ebededad7471f3a15b0bf2c5c"
FFMPEG = Path(
    os.environ.get("RENDER_FFMPEG_PATH", str(Path.home() / ".local/share/avp/ffmpeg/7.1.1/ffmpeg"))
)
FONT = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")

pytestmark = pytest.mark.skipif(
    not (FFMPEG.is_file() and FONT.is_file()), reason="static ffmpeg or Noto CJK not installed"
)


def _gen(out: Path, *args: str) -> Path:
    subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", *args, str(out)],
        check=True,
        timeout=60,
    )
    return out


@pytest.fixture(scope="module")
def sources(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("render-src")
    video = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    return {
        "a": _gen(d / "a.mp4", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=24:duration=1.2", *video),  # noqa: E501
        "b": _gen(d / "b.mp4", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30:duration=0.8", *video),  # noqa: E501
        "long": _gen(d / "long.mp4", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30:duration=60", *video),  # noqa: E501
        "v1": _gen(d / "v1.wav", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=22050:duration=0.9"),  # noqa: E501
        "v2": _gen(d / "v2.wav", "-f", "lavfi", "-i", "sine=frequency=660:sample_rate=22050:duration=0.7"),  # noqa: E501
    }  # fmt: skip


@pytest.fixture(scope="module")
def engine() -> FfmpegRenderEngine:
    return FfmpegRenderEngine.from_binary(FFMPEG, FFMPEG_SHA256)


def _request(engine, sources, work: Path, profile: str, *, subtitles: bool = True) -> RenderRequest:
    plan = make_plan(
        profile=profile,
        scenes=((1200, 1000), (800, 1000)),  # trim と freeze_tail
        voices=((0, 900), (1100, 700)),
        cues=((0, 0, 900), (1, 1100, 1800)) if subtitles else (),
        engine=engine.identity(),
        subtitles=subtitles,
    )
    return RenderRequest(
        plan=plan,
        scene_video_paths={"sb1": sources["a"], "sb2": sources["b"]},
        voice_paths={"s1": sources["v1"], "s2": sources["v2"]},
        subtitle_texts=["こんにちは世界 Hello", "字幕テスト 漢字"][: len(plan.subtitle_cues)],
        font_path=FONT,
        work_dir=work,
        output_path=work / "final.mp4",
        timeout_seconds=120,
    )


@pytest.mark.parametrize("profile", ["shorts_vertical", "long_form_horizontal"])
async def test_renders_the_profile_and_measures_as_planned(
    engine, sources, tmp_path: Path, profile: str
) -> None:
    request = _request(engine, sources, tmp_path / "w", profile)
    rendered = await engine.render(request, heartbeat=lambda: None)
    info = PillowAvMediaProbe().probe_final_video(str(rendered.path))
    plan = request.plan
    assert (info.width, info.height) == (plan.profile.width, plan.profile.height)
    assert info.fps_millis == plan.profile.fps_millis
    assert (info.video_codec, info.pix_fmt, info.audio_codec) == ("h264", "yuv420p", "aac")
    assert (info.audio_sample_rate_hz, info.audio_channels) == (48_000, 2)
    tolerance = plan.profile.limits.duration_tolerance_ms
    assert abs(info.duration_ms - plan.total_duration_ms) <= tolerance
    assert info.audio_duration_ms is not None
    assert abs(info.audio_duration_ms - plan.total_duration_ms) <= tolerance
    assert info.frames_decoded == 60 and info.decode_errors == 0
    assert info.bytes == rendered.bytes


async def test_same_inputs_render_byte_identical_output(engine, sources, tmp_path: Path) -> None:
    digests = []
    for run in range(2):
        request = _request(engine, sources, tmp_path / f"w{run}", "shorts_vertical")
        rendered = await engine.render(request, heartbeat=lambda: None)
        digests.append(hashlib.sha256(rendered.path.read_bytes()).hexdigest())
    assert digests[0] == digests[1]


async def test_subtitles_are_burned_with_the_configured_font(
    engine, sources, tmp_path: Path
) -> None:
    """同じ時刻のフレームで、字幕ありと無しの下部領域が異なる（グリフが描かれている）。"""
    frames = []
    for subtitles in (True, False):
        request = _request(
            engine, sources, tmp_path / str(subtitles), "long_form_horizontal", subtitles=subtitles
        )
        rendered = await engine.render(request, heartbeat=lambda: None)
        with av.open(str(rendered.path)) as container:
            frame = next(f for i, f in enumerate(container.decode(video=0)) if i == 15)
            frames.append(frame.to_image().convert("L"))
    with_subs, without = frames
    band = (0, 1080 - 86 - 70, 1920, 1080 - 86 + 5)  # margin_bottom の上に字幕の行がある
    diff = ImageChops.difference(with_subs.crop(band), without.crop(band))
    changed = sum(diff.histogram()[65:256])  # 輝度差が 64 を超える画素の数
    assert changed > 500
    ass = (tmp_path / "True" / "subtitles.ass").read_text(encoding="utf-8")
    assert "Style: Default,Noto Sans CJK JP," in ass


def _processes_mentioning(token: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            state = (entry / "stat").read_text().split()[2]
        except OSError:
            continue
        if token in cmdline and state != "Z":
            found.append(int(entry.name))
    return found


async def test_cancellation_stops_ffmpeg_and_leaves_no_output(
    engine, sources, tmp_path: Path
) -> None:
    work = tmp_path / "cancel"
    request = _request(engine, sources, work, "shorts_vertical", subtitles=False)
    plan = make_plan(
        profile="shorts_vertical",
        scenes=((60_000, 60_000),),
        voices=((0, 900),),
        engine=engine.identity(),
        subtitles=False,
    )
    request = RenderRequest(
        plan=plan,
        scene_video_paths={"sb1": sources["long"]},
        voice_paths={"s1": sources["v1"]},
        subtitle_texts=[],
        font_path=FONT,
        work_dir=work,
        output_path=work / "final.mp4",
        timeout_seconds=300,
    )
    token = str((work / "final.mp4.partial.mp4").resolve())
    task = asyncio.create_task(engine.render(request, heartbeat=lambda: None))
    for _ in range(100):
        if _processes_mentioning(token):
            break
        await asyncio.sleep(0.05)
    assert _processes_mentioning(token), "ffmpeg did not start"
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _processes_mentioning(token) == []
    assert not (work / "final.mp4").exists()
    assert not (work / "final.mp4.partial.mp4").exists()
