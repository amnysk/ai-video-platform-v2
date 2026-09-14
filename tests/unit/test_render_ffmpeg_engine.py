"""FfmpegRenderEngine の分岐（子プロセスは差し替え）: 結果の写像・事前検査・部分出力の後始末。"""

from __future__ import annotations

import asyncio
import hashlib
import stat
from pathlib import Path

import pytest

from contracts.render import RenderEngineIdentity
from domain.errors import (
    RenderEngineFailedError,
    RenderEngineTimeoutError,
    RenderEngineUnavailableError,
    RenderWorkspaceFullError,
)
from domain.render.ports import RenderEngine, RenderRequest
from infrastructure.render.binary import BinaryIdentity
from infrastructure.render.ffmpeg_engine import FfmpegRenderEngine
from infrastructure.render.process import ProcessOutcome, SupervisedProcessRunner, SupervisedResult
from tests.support.render_plans import make_plan


class StubRunner(SupervisedProcessRunner):
    def __init__(self, outcome: ProcessOutcome, *, write: bytes | None = b"mp4", delay=0.0):
        super().__init__()
        self.outcome = outcome
        self.write = write
        self.delay = delay
        self.calls: list[dict] = []

    async def run(self, argv, **kw):  # type: ignore[override]
        self.calls.append({"argv": list(argv), **kw})
        kw["heartbeat"]()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.write is not None:
            Path(argv[-1]).write_bytes(self.write)
        return SupervisedResult(
            outcome=self.outcome,
            exit_code=0 if self.outcome is ProcessOutcome.SUCCEEDED else 1,
            signal=9 if self.outcome is ProcessOutcome.SIGNALED else None,
            duration_seconds=0.1,
            stdout_path=kw["log_dir"] / "o",
            stderr_path=kw["log_dir"] / "e",
            stderr_tail="x" * 5000 + "TAIL",
        )


def _binary(tmp_path: Path) -> BinaryIdentity:
    path = tmp_path / "ffmpeg"
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    return BinaryIdentity(engine="ffmpeg", version="7.1.1", binary_sha256=sha, path=path)


def _request(tmp_path: Path, engine: FfmpegRenderEngine, **plan_kw) -> RenderRequest:
    plan = make_plan(engine=engine.identity(), **plan_kw)
    for name in ("a.mp4", "v.wav"):
        (tmp_path / name).write_bytes(b"x")
    return RenderRequest(
        plan=plan,
        scene_video_paths={"sb1": tmp_path / "a.mp4"},
        voice_paths={"s1": tmp_path / "v.wav"},
        subtitle_texts=["字幕"] * len(plan.subtitle_cues),
        font_path=Path("/nonexistent/font.ttc"),
        work_dir=tmp_path / "work",
        output_path=tmp_path / "final.mp4",
        timeout_seconds=10,
    )


def _engine(tmp_path: Path, runner: StubRunner) -> FfmpegRenderEngine:
    return FfmpegRenderEngine(_binary(tmp_path), threads=2, runner=runner)


def test_satisfies_the_port(tmp_path: Path) -> None:
    engine = _engine(tmp_path, StubRunner(ProcessOutcome.SUCCEEDED))
    assert isinstance(engine, RenderEngine)
    assert engine.identity().engine == "ffmpeg"


async def test_success_moves_the_partial_output_into_place(tmp_path: Path) -> None:
    runner = StubRunner(ProcessOutcome.SUCCEEDED)
    engine = _engine(tmp_path, runner)
    beats: list[int] = []
    request = _request(tmp_path, engine, subtitles=False)
    rendered = await engine.render(request, heartbeat=lambda: beats.append(1))
    assert rendered.path == request.output_path and rendered.bytes == 3
    assert request.output_path.read_bytes() == b"mp4"
    assert not list(tmp_path.glob("*.partial.mp4"))
    call = runner.calls[0]
    assert call["cwd"] == request.work_dir and call["timeout_seconds"] == 10
    assert call["env"]["HOME"] == str(request.work_dir.resolve())
    assert (request.work_dir / "filtergraph.txt").is_file()
    assert len(beats) >= 3


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        (ProcessOutcome.FAILED, RenderEngineFailedError),
        (ProcessOutcome.SIGNALED, RenderEngineFailedError),
        (ProcessOutcome.TIMED_OUT, RenderEngineTimeoutError),
        (ProcessOutcome.NO_SPACE, RenderWorkspaceFullError),
    ],
)
async def test_outcomes_map_to_domain_errors(tmp_path: Path, outcome, error) -> None:
    engine = _engine(tmp_path, StubRunner(outcome))
    request = _request(tmp_path, engine, subtitles=False)
    with pytest.raises(error) as info:
        await engine.render(request, heartbeat=lambda: None)
    message = str(info.value)
    assert message.endswith("TAIL") and len(message) < 2200
    assert not request.output_path.exists()
    assert not list(tmp_path.glob("*.partial.mp4"))


async def test_empty_output_is_a_failure(tmp_path: Path) -> None:
    engine = _engine(tmp_path, StubRunner(ProcessOutcome.SUCCEEDED, write=b""))
    with pytest.raises(RenderEngineFailedError):
        await engine.render(_request(tmp_path, engine, subtitles=False), heartbeat=lambda: None)


async def test_cancellation_removes_the_partial_output(tmp_path: Path) -> None:
    engine = _engine(tmp_path, StubRunner(ProcessOutcome.SUCCEEDED, delay=5))
    request = _request(tmp_path, engine, subtitles=False)
    task = asyncio.create_task(engine.render(request, heartbeat=lambda: None))
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not list(tmp_path.glob("*.partial.mp4")) and not request.output_path.exists()


async def test_plan_for_another_engine_is_refused(tmp_path: Path) -> None:
    runner = StubRunner(ProcessOutcome.SUCCEEDED)
    engine = _engine(tmp_path, runner)
    request = _request(tmp_path, engine, subtitles=False)
    other = RenderEngineIdentity(engine="ffmpeg", version="7.1.1", binary_sha256="c" * 64)
    request = _replace_plan(request, other)
    with pytest.raises(RenderEngineUnavailableError):
        await engine.render(request, heartbeat=lambda: None)
    assert not runner.calls


def _replace_plan(request: RenderRequest, engine: RenderEngineIdentity) -> RenderRequest:
    import dataclasses

    return dataclasses.replace(request, plan=request.plan.model_copy(update={"engine": engine}))


async def test_binary_changed_after_verification_is_refused(tmp_path: Path) -> None:
    runner = StubRunner(ProcessOutcome.SUCCEEDED)
    engine = _engine(tmp_path, runner)
    request = _request(tmp_path, engine, subtitles=False)
    (tmp_path / "ffmpeg").write_text("#!/bin/sh\necho tampered\n")
    with pytest.raises(RenderEngineUnavailableError):
        await engine.render(request, heartbeat=lambda: None)
    assert not runner.calls


async def test_missing_font_is_unavailable_when_subtitles_are_burned(tmp_path: Path) -> None:
    runner = StubRunner(ProcessOutcome.SUCCEEDED)
    engine = _engine(tmp_path, runner)
    request = _request(tmp_path, engine, cues=((0, 0, 500),))
    with pytest.raises(RenderEngineUnavailableError, match="font"):
        await engine.render(request, heartbeat=lambda: None)
    assert not runner.calls


async def test_missing_inputs_or_misaligned_texts_are_programming_errors(tmp_path: Path) -> None:
    import dataclasses

    engine = _engine(tmp_path, StubRunner(ProcessOutcome.SUCCEEDED))
    request = _request(tmp_path, engine, subtitles=False)
    with pytest.raises(ValueError):
        await engine.render(
            dataclasses.replace(request, scene_video_paths={}), heartbeat=lambda: None
        )
    with pytest.raises(ValueError):
        await engine.render(
            dataclasses.replace(request, subtitle_texts=["x"]), heartbeat=lambda: None
        )


def test_from_binary_maps_verification_failure(tmp_path: Path) -> None:
    with pytest.raises(RenderEngineUnavailableError):
        FfmpegRenderEngine.from_binary(tmp_path / "missing", "0" * 64)
