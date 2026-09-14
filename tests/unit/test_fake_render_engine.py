"""テスト用フェイクが port を満たし、約束どおりに振る舞う。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from domain.errors import RenderEngineFailedError
from domain.render.ports import FinalVideoProbe, RenderEngine, RenderRequest
from tests.support.fake_render_engine import FakeFinalVideoProbe, FakeRenderEngine
from tests.support.render_plans import make_plan


def _request(tmp_path: Path) -> RenderRequest:
    return RenderRequest(
        plan=make_plan(),
        scene_video_paths={"sb1": tmp_path / "a.mp4"},
        voice_paths={"s1": tmp_path / "v.wav"},
        subtitle_texts=[],
        font_path=tmp_path / "font.ttc",
        work_dir=tmp_path / "work",
        output_path=tmp_path / "out" / "final.mp4",
        timeout_seconds=5,
    )


async def test_fake_engine_records_writes_and_fails_in_order(tmp_path: Path) -> None:
    engine = FakeRenderEngine(failures=[RenderEngineFailedError("boom")])
    assert isinstance(engine, RenderEngine)
    request = _request(tmp_path)
    with pytest.raises(RenderEngineFailedError):
        await engine.render(request, heartbeat=lambda: None)
    rendered = await engine.render(request, heartbeat=lambda: None)
    assert rendered.path.read_bytes() == engine.payload
    assert len(engine.requests) == 2 and engine.completed == 1


async def test_fake_engine_honors_cancellation(tmp_path: Path) -> None:
    engine = FakeRenderEngine(delay_seconds=10)
    task = asyncio.create_task(engine.render(_request(tmp_path), heartbeat=lambda: None))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.cancelled and engine.completed == 0


def test_fake_probe_is_consistent_with_the_plan_and_overridable(tmp_path: Path) -> None:
    plan = make_plan(profile="long_form_horizontal", scenes=((2000, 2000),))
    probe = FakeFinalVideoProbe(plan)
    assert isinstance(probe, FinalVideoProbe)
    info = probe.probe_final_video(str(tmp_path / "x.mp4"))
    assert (info.width, info.height, info.duration_ms, info.frames_decoded) == (
        1920,
        1080,
        2000,
        60,
    )
    broken = FakeFinalVideoProbe(plan, audio_present=False, audio_codec=None)
    assert not broken.probe_final_video("x").audio_present
