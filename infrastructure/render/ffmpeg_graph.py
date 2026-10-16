"""描画計画から ffmpeg の filtergraph と argv を組む（純粋関数、ADR-0019）。

映像（シーンごと）:
  切り出し（trim）→ 時刻を 0 起点に → profile の fps へ → 収め方（cover: 拡大して中央切り抜き /
  contain: 縮小して黒で余白）→ SAR 1 → yuv420p → 最終フレームを保持して延長（tpad clone）→
  時間軸上の尺ちょうどに切る。延長には freeze_ms に加えて ``SCENE_SAFETY_PAD_MS`` を足し、
  宣言上の実尺よりデコードできた尺が数フレーム短くても、シーンの尺が計画から縮まないようにする。
  その後 concat でつなぐ（5A はカットのみ）。
音声:
  総尺の無音を土台に、各音声を profile のサンプルレート・ch へ揃え、尺で切り、開始位置へ遅延し、
  正規化なし（``normalize=0``）で合算する。ラウドネス補正はしない（決定性）。
字幕:
  作業領域の ASS を ``ass`` filter で焼き込む。パスは作業領域からの相対名だけを使い、
  filtergraph の escape 規則に物理パスを通さない。
"""

from __future__ import annotations

from collections.abc import Sequence

from contracts.render import RenderPlan, RenderProfile, RenderTimelineScene

#: freeze 以外にも末尾へ足す保持の余裕（最後に尺ちょうどで切るので出力の尺は変わらない）
SCENE_SAFETY_PAD_MS = 500
#: 作業領域（子プロセスの cwd）からの相対名
SUBTITLES_FILE_NAME = "subtitles.ass"
FONTS_DIR_NAME = "fonts"
FILTERGRAPH_FILE_NAME = "filtergraph.txt"

_CHANNEL_LAYOUTS = {1: "mono", 2: "stereo"}


def seconds(ms: int) -> str:
    """int ミリ秒を丸め誤差の無い10進の秒表記へ。"""
    return f"{ms // 1000}.{ms % 1000:03d}"


def fps_expr(profile: RenderProfile) -> str:
    return f"{profile.fps_millis}/1000"


def should_burn_subtitles(plan: RenderPlan) -> bool:
    return plan.profile.subtitles.enabled and bool(plan.subtitle_cues)


def _fit(profile: RenderProfile) -> str:
    w, h = profile.width, profile.height
    if profile.fit == "cover":
        return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}"
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def _scene_chain(index: int, scene: RenderTimelineScene, profile: RenderProfile) -> str:
    used_ms = scene.source_duration_ms - scene.reconciliation.trim_ms
    hold_ms = scene.reconciliation.freeze_ms + SCENE_SAFETY_PAD_MS
    return (
        f"[{index}:v:0]trim=start=0:duration={seconds(used_ms)},setpts=PTS-STARTPTS,"
        f"fps={fps_expr(profile)},{_fit(profile)},setsar=1,format=yuv420p,"
        f"tpad=stop_mode=clone:stop_duration={seconds(hold_ms)},"
        f"trim=duration={seconds(scene.timeline_duration_ms)},setpts=PTS-STARTPTS[v{index}]"
    )


def build_filtergraph(plan: RenderPlan) -> str:
    profile = plan.profile
    audio = profile.audio
    layout = _CHANNEL_LAYOUTS[audio.channels]
    total = seconds(plan.total_duration_ms)
    n_scenes = len(plan.scenes)
    lines = [_scene_chain(i, scene, profile) for i, scene in enumerate(plan.scenes)]
    labels = "".join(f"[v{i}]" for i in range(n_scenes))
    lines.append(f"{labels}concat=n={n_scenes}:v=1:a=0[vcat]")
    if should_burn_subtitles(plan):
        lines.append(
            f"[vcat]ass=filename={SUBTITLES_FILE_NAME}:fontsdir={FONTS_DIR_NAME}:"
            "shaping=complex[vout]"
        )
    else:
        lines.append("[vcat]null[vout]")

    lines.append(f"anullsrc=r={audio.sample_rate_hz}:cl={layout},atrim=duration={total}[abase]")
    for k, voice in enumerate(plan.voices):
        lines.append(
            f"[{n_scenes + k}:a:0]aresample={audio.sample_rate_hz},"
            f"aformat=sample_fmts=fltp:sample_rates={audio.sample_rate_hz}:channel_layouts={layout},"
            f"atrim=duration={seconds(voice.duration_ms)},asetpts=PTS-STARTPTS,"
            f"adelay=delays={voice.start_ms}:all=1[a{k}]"
        )
    voice_labels = "".join(f"[a{k}]" for k in range(len(plan.voices)))
    lines.append(
        f"[abase]{voice_labels}amix=inputs={len(plan.voices) + 1}:duration=first:"
        f"dropout_transition=0:normalize=0,atrim=duration={total}[aout]"
    )
    return ";\n".join(lines) + "\n"


def build_argv(
    *,
    ffmpeg: str,
    plan: RenderPlan,
    scene_paths: Sequence[str],
    voice_paths: Sequence[str],
    output: str,
    threads: int,
) -> list[str]:
    """argv を組む。

    入力の順は ``plan.scenes`` → ``plan.voices``（filtergraph の入力番号と一致）。
    """
    if len(scene_paths) != len(plan.scenes) or len(voice_paths) != len(plan.voices):
        raise ValueError("input paths must align with plan scenes and voices")
    if threads <= 0:
        raise ValueError("threads must be positive")
    profile = plan.profile
    video, audio = profile.video, profile.audio
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y"]
    for path in scene_paths:
        argv += ["-an", "-sn", "-dn", "-i", path]
    for path in voice_paths:
        argv += ["-vn", "-sn", "-dn", "-i", path]
    argv += ["-/filter_complex", FILTERGRAPH_FILE_NAME, "-filter_complex_threads", str(threads)]
    argv += ["-map", "[vout]", "-map", "[aout]"]
    argv += ["-c:v", "libx264", "-preset", video.preset, "-crf", str(video.crf)]
    argv += ["-pix_fmt", video.pix_fmt, "-r", fps_expr(profile), "-fps_mode", "cfr"]
    if video.gop_frames is not None:
        argv += ["-g", str(video.gop_frames)]
    argv += ["-c:a", "aac", "-b:a", f"{audio.bitrate_kbps}k"]
    argv += ["-ar", str(audio.sample_rate_hz), "-ac", str(audio.channels)]
    argv += ["-threads", str(threads)]
    argv += ["-map_metadata", "-1", "-map_chapters", "-1"]
    argv += ["-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
    argv += ["-movflags", "+faststart", "-t", seconds(plan.total_duration_ms), "-f", "mp4", output]
    return argv


__all__ = [
    "FILTERGRAPH_FILE_NAME",
    "FONTS_DIR_NAME",
    "SCENE_SAFETY_PAD_MS",
    "SUBTITLES_FILE_NAME",
    "build_argv",
    "build_filtergraph",
    "fps_expr",
    "seconds",
    "should_burn_subtitles",
]
