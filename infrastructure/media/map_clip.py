"""地図 PNG を既存の render 線のシーン映像（mp4）にする ffmpeg の argv（ADR-0078）。純粋関数。

既存の ``ffmpeg_graph`` の fps・収め方（``fit_filter``）・秒表記を**再利用**し、シーン映像と同じ
符号化設定（profile）で静止画を指定の尺だけ映像にする。実行（子プロセス）はここではしない。
"""

from __future__ import annotations

from contracts.render import RenderProfile
from infrastructure.render.ffmpeg_graph import fit_filter, fps_expr, seconds


def build_map_clip_argv(
    ffmpeg: str,
    png_name: str,
    output_name: str,
    duration_ms: int,
    profile: RenderProfile,
) -> list[str]:
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    video = profile.video
    vf = f"{fit_filter(profile)},setsar=1,format={video.pix_fmt}"
    argv = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y"]
    argv += ["-loop", "1", "-framerate", fps_expr(profile), "-i", png_name]
    argv += ["-vf", vf, "-r", fps_expr(profile), "-fps_mode", "cfr", "-t", seconds(duration_ms)]
    argv += ["-an", "-sn", "-dn"]
    argv += ["-c:v", "libx264", "-preset", video.preset, "-crf", str(video.crf)]
    argv += ["-pix_fmt", video.pix_fmt]
    if video.gop_frames is not None:
        argv += ["-g", str(video.gop_frames)]
    argv += ["-threads", "1", "-map_metadata", "-1", "-map_chapters", "-1"]
    argv += ["-fflags", "+bitexact", "-flags:v", "+bitexact"]
    argv += ["-movflags", "+faststart", "-f", "mp4", output_name]
    return argv


__all__ = ["build_map_clip_argv"]
