"""発話中の声と BGM のラウドネスを実測する（ADR-0083 §5）。固定版 ffmpeg の子プロセス。

本番と同じ filtergraph 部品（fade・level・ducking）で「ナレーションだけ」「BGM だけ」の 2 本の
音声を作業領域に書き出し、各台本 scene の発話区間（``RenderVoicePlacement``）で ``ebur128`` を測る。
成果物（完成動画）は作らない。判定は ``domain.music.intelligibility`` が行う。
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from contracts.render import RenderPlan
from domain.errors import RenderEngineFailedError
from domain.music.intelligibility import SceneSpeechLevels
from domain.render.loudness import (
    SILENT_INTEGRATED_CENTI,
    LoudnessParseError,
    parse_ebur128_summary,
)
from domain.render.ports import BgmRenderInput
from infrastructure.render.ffmpeg_graph import (
    build_bgm_stem_filtergraph,
    build_voice_stem_filtergraph,
    seconds,
)

STEM_TIMEOUT_SECONDS = 300
_ENV = {"PATH": "/usr/bin:/bin", "LC_ALL": "C.UTF-8"}


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        done = subprocess.run(  # noqa: S603 - 固定版 ffmpeg の絶対パス、argv は固定
            argv, capture_output=True, text=True, timeout=timeout, env=_ENV, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RenderEngineFailedError(f"bgm stem measurement could not run: {exc}") from exc
    if done.returncode != 0:
        raise RenderEngineFailedError(f"bgm stem measurement failed: {done.stderr[-500:]}")
    return done


class FfmpegSpeechMarginProbe:
    def __init__(self, ffmpeg: str | Path, *, timeout_seconds: int = STEM_TIMEOUT_SECONDS) -> None:
        self._ffmpeg = str(ffmpeg)
        self._timeout = timeout_seconds

    def _write_stem(
        self, graph: str, inputs: list[list[str]], out: Path, work: Path, tag: str
    ) -> None:
        graph_file = work / f"{tag}.txt"
        graph_file.write_text(graph, encoding="utf-8")
        argv = [self._ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y"]
        for fragment in inputs:
            argv += fragment
        argv += ["-/filter_complex", str(graph_file), "-map", "[aout]", "-c:a", "pcm_s16le"]
        argv += [str(out)]
        _run(argv, self._timeout)

    def window_lufs(self, wav: Path, start_ms: int, duration_ms: int) -> int | None:
        """``wav`` の区間の積分ラウドネス（1/100 LUFS）。無音は ``None``。"""
        argv = [
            self._ffmpeg, "-hide_banner", "-nostdin", "-nostats",
            "-ss", seconds(start_ms), "-t", seconds(duration_ms), "-i", str(wav),
            "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-",
        ]  # fmt: skip
        done = _run(argv, self._timeout)
        try:
            integrated = parse_ebur128_summary(done.stderr).integrated_lufs_centi
        except LoudnessParseError as exc:
            raise RenderEngineFailedError(f"window loudness unreadable: {exc}") from exc
        if integrated is None or integrated <= SILENT_INTEGRATED_CENTI:
            return None
        return integrated

    def write_stems(
        self, plan: RenderPlan, bgm: BgmRenderInput, voice_paths: Mapping[str, Path], out_dir: Path
    ) -> tuple[Path, Path]:
        """``out_dir`` へ ``voice.wav`` と ``music.wav``（ducking 後）を書く。"""
        voice_inputs = [
            ["-vn", "-sn", "-dn", "-i", str(Path(voice_paths[v.script_scene_id]).resolve())]
            for v in plan.voices
        ]
        cue_inputs = [
            [
                "-stream_loop",
                "-1",
                "-vn",
                "-sn",
                "-dn",
                "-i",
                str(Path(bgm.track_paths[c.track_id]).resolve()),
            ]  # fmt: skip
            for c in bgm.mix.cues
        ]
        out_dir.mkdir(parents=True, exist_ok=True)
        voice_wav, music_wav = out_dir / "voice.wav", out_dir / "music.wav"
        self._write_stem(
            build_voice_stem_filtergraph(plan), voice_inputs, voice_wav, out_dir, "voice"
        )
        self._write_stem(
            build_bgm_stem_filtergraph(plan, bgm.mix),
            [*voice_inputs, *cue_inputs],
            music_wav,
            out_dir,
            "music",
        )
        return voice_wav, music_wav

    def measure(
        self, plan: RenderPlan, bgm: BgmRenderInput, voice_paths: Mapping[str, Path]
    ) -> tuple[SceneSpeechLevels, ...]:
        with tempfile.TemporaryDirectory(prefix="bgm-stems-") as tmp:
            voice_wav, music_wav = self.write_stems(plan, bgm, voice_paths, Path(tmp))
            return tuple(
                SceneSpeechLevels(
                    scene_id=v.script_scene_id,
                    voice_lufs_centi=self.window_lufs(voice_wav, v.start_ms, v.duration_ms),
                    music_lufs_centi=self.window_lufs(music_wav, v.start_ms, v.duration_ms),
                )
                for v in plan.voices
            )


__all__ = ["STEM_TIMEOUT_SECONDS", "FfmpegSpeechMarginProbe"]
