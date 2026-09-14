"""``domain.render.ports.RenderEngine`` の実装（固定版 static ffmpeg の子プロセス、ADR-0019）。

- 起動前に毎回バイナリの sha256 を照合する（差し替わっていたら描かない）
- 計画の engine identity と、このエンジンの identity が一致しなければ描かない
- 出力はまず ``<output>.partial.mp4`` に書き、成功したときだけ ``os.replace`` で置く
- 子プロセスの環境は最小限（PATH / LC_ALL / HOME=作業領域）。
  fontconfig のキャッシュも作業領域に閉じる
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from contracts.render import DEFAULT_RENDER_FFMPEG_THREADS, RenderEngineIdentity
from contracts.render_activities import RENDER_CANCEL_GRACE_SECONDS
from domain.errors import (
    RenderEngineFailedError,
    RenderEngineTimeoutError,
    RenderEngineUnavailableError,
    RenderWorkspaceFullError,
)
from domain.render.ports import RenderedVideo, RenderRequest
from infrastructure.render.ass import FontReadError, build_ass, read_font_family
from infrastructure.render.binary import (
    BinaryIdentity,
    RenderBinaryError,
    file_sha256,
    verify_binary,
)
from infrastructure.render.ffmpeg_graph import (
    FILTERGRAPH_FILE_NAME,
    FONTS_DIR_NAME,
    SUBTITLES_FILE_NAME,
    build_argv,
    build_filtergraph,
    should_burn_subtitles,
)
from infrastructure.render.process import ProcessOutcome, SupervisedProcessRunner

#: 例外メッセージに含める stderr 末尾の上限（文字）
ERROR_TAIL_CHARS = 2_000
LOG_NAME = "ffmpeg"


class FfmpegRenderEngine:
    def __init__(
        self,
        binary: BinaryIdentity,
        *,
        threads: int = DEFAULT_RENDER_FFMPEG_THREADS,
        runner: SupervisedProcessRunner | None = None,
    ) -> None:
        if threads <= 0:
            raise ValueError("threads must be positive")
        self._binary = binary
        self._threads = threads
        self._runner = runner or SupervisedProcessRunner(grace_seconds=RENDER_CANCEL_GRACE_SECONDS)

    @classmethod
    def from_binary(
        cls,
        path: str | Path,
        sha256: str,
        *,
        threads: int = DEFAULT_RENDER_FFMPEG_THREADS,
        runner: SupervisedProcessRunner | None = None,
    ) -> FfmpegRenderEngine:
        """バイナリを検証して組む。検証できなければ ``RenderEngineUnavailableError``。"""
        try:
            binary = verify_binary(path, sha256, engine="ffmpeg")
        except RenderBinaryError as exc:
            raise RenderEngineUnavailableError(str(exc)) from exc
        return cls(binary, threads=threads, runner=runner)

    def identity(self) -> RenderEngineIdentity:
        return RenderEngineIdentity(
            engine=self._binary.engine,
            version=self._binary.version,
            binary_sha256=self._binary.binary_sha256,
        )

    async def render(
        self, request: RenderRequest, *, heartbeat: Callable[[], object]
    ) -> RenderedVideo:
        plan = request.plan
        if plan.engine != self.identity():
            raise RenderEngineUnavailableError(
                "render plan was built for a different engine identity"
            )
        scene_paths = [_require(request.scene_video_paths, s.scene_id) for s in plan.scenes]
        voice_paths = [_require(request.voice_paths, v.script_scene_id) for v in plan.voices]
        if len(request.subtitle_texts) != len(plan.subtitle_cues):
            raise ValueError("subtitle_texts must align with plan.subtitle_cues")

        heartbeat()
        actual = await asyncio.to_thread(_sha_or_none, self._binary.path)
        if actual != self._binary.binary_sha256:
            raise RenderEngineUnavailableError(
                f"ffmpeg binary changed or disappeared since verification: {self._binary.path}"
            )
        heartbeat()

        work_dir = request.work_dir
        work_dir.mkdir(parents=True, exist_ok=True)
        if should_burn_subtitles(plan):
            self._prepare_subtitles(request)
        (work_dir / FILTERGRAPH_FILE_NAME).write_text(build_filtergraph(plan), encoding="utf-8")

        output = request.output_path
        partial = output.with_name(output.name + ".partial.mp4")
        argv = build_argv(
            ffmpeg=str(self._binary.path),
            plan=plan,
            scene_paths=[str(p.resolve()) for p in scene_paths],
            voice_paths=[str(p.resolve()) for p in voice_paths],
            output=str(partial.resolve()),
            threads=self._threads,
        )
        env = {
            "PATH": "/usr/bin:/bin",
            "LC_ALL": "C.UTF-8",
            "HOME": str(work_dir.resolve()),
            "XDG_CACHE_HOME": str((work_dir / ".cache").resolve()),
        }
        try:
            result = await self._runner.run(
                argv,
                log_dir=work_dir,
                log_name=LOG_NAME,
                timeout_seconds=request.timeout_seconds,
                heartbeat=heartbeat,
                env=env,
                cwd=work_dir,
            )
        except BaseException:
            _unlink(partial)
            raise
        tail = result.stderr_tail[-ERROR_TAIL_CHARS:]
        if result.outcome is not ProcessOutcome.SUCCEEDED:
            _unlink(partial)
        if result.outcome is ProcessOutcome.TIMED_OUT:
            raise RenderEngineTimeoutError(
                f"ffmpeg exceeded {request.timeout_seconds}s and was stopped: {tail}"
            )
        if result.outcome is ProcessOutcome.NO_SPACE:
            raise RenderWorkspaceFullError(f"no space left while rendering: {tail}")
        if result.outcome is ProcessOutcome.SIGNALED:
            raise RenderEngineFailedError(f"ffmpeg killed by signal {result.signal}: {tail}")
        if result.outcome is ProcessOutcome.FAILED:
            raise RenderEngineFailedError(f"ffmpeg exited with {result.exit_code}: {tail}")
        try:
            size = partial.stat().st_size
        except FileNotFoundError as exc:
            raise RenderEngineFailedError("ffmpeg succeeded but wrote no output") from exc
        if size <= 0:
            _unlink(partial)
            raise RenderEngineFailedError("ffmpeg succeeded but the output is empty")
        os.replace(partial, output)
        heartbeat()
        return RenderedVideo(path=output, bytes=size)

    @staticmethod
    def _prepare_subtitles(request: RenderRequest) -> None:
        font = request.font_path
        if not font.is_file():
            raise RenderEngineUnavailableError(f"subtitle font not found: {font}")
        try:
            family = read_font_family(font)
        except FontReadError as exc:
            raise RenderEngineUnavailableError(str(exc)) from exc
        fonts_dir = request.work_dir / FONTS_DIR_NAME
        # 設定されたフォント1つだけを置く（システムの別フォントに置き換わらないように）
        if fonts_dir.exists():
            shutil.rmtree(fonts_dir)
        fonts_dir.mkdir()
        (fonts_dir / font.name).symlink_to(font.resolve())
        ass = build_ass(
            request.plan.profile,
            request.plan.subtitle_cues,
            request.subtitle_texts,
            font_family=family,
        )
        (request.work_dir / SUBTITLES_FILE_NAME).write_text(ass, encoding="utf-8")


def _require(paths: Mapping[str, Path], key: str) -> Path:
    try:
        return Path(paths[key])
    except KeyError:
        raise ValueError(f"no input file for {key}") from None


def _sha_or_none(path: Path) -> str | None:
    try:
        return file_sha256(path)
    except OSError:
        return None


def _unlink(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


__all__ = ["ERROR_TAIL_CHARS", "FfmpegRenderEngine"]
