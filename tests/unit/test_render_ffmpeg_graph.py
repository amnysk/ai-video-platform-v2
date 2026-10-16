"""filtergraph / argv の組み立て（純粋関数）の snapshot。"""

from __future__ import annotations

import pytest

from infrastructure.render.ffmpeg_graph import build_argv, build_filtergraph, seconds
from tests.support.render_plans import make_plan

SHORTS_TRIM_FREEZE_NO_SUBS = """\
[0:v:0]trim=start=0:duration=1.000,setpts=PTS-STARTPTS,fps=30000/1000,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=0.500,trim=duration=1.000,setpts=PTS-STARTPTS[v0];
[1:v:0]trim=start=0:duration=0.800,setpts=PTS-STARTPTS,fps=30000/1000,scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=0.700,trim=duration=1.000,setpts=PTS-STARTPTS[v1];
[v0][v1]concat=n=2:v=1:a=0[vcat];
[vcat]null[vout];
anullsrc=r=48000:cl=stereo,atrim=duration=2.000[abase];
[2:a:0]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=duration=0.900,asetpts=PTS-STARTPTS,adelay=delays=0:all=1[a0];
[3:a:0]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=duration=0.700,asetpts=PTS-STARTPTS,adelay=delays=1100:all=1[a1];
[abase][a0][a1]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,atrim=duration=2.000[aout]
"""  # noqa: E501


def _two_scene_plan(profile: str, *, subtitles: bool | None = None, cues=()):
    return make_plan(
        profile=profile,
        scenes=((1200, 1000), (800, 1000)),
        voices=((0, 900), (1100, 700)),
        cues=cues,
        subtitles=subtitles,
    )


def test_shorts_trim_and_freeze_without_subtitles_snapshot() -> None:
    plan = _two_scene_plan("shorts_vertical", subtitles=False)
    assert plan.scenes[0].reconciliation.mode == "trim"
    assert plan.scenes[1].reconciliation.mode == "freeze_tail"
    assert build_filtergraph(plan) == SHORTS_TRIM_FREEZE_NO_SUBS


def test_long_form_contains_with_black_padding_and_burns_subtitles() -> None:
    plan = _two_scene_plan("long_form_horizontal", cues=((0, 0, 900), (1, 1100, 1800)))
    graph = build_filtergraph(plan)
    fit = (
        "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    assert graph.count(fit) == 2
    assert "crop=" not in graph
    assert "[vcat]ass=filename=subtitles.ass:fontsdir=fonts:shaping=complex[vout];" in graph
    assert "[vcat]null" not in graph


def test_enabled_subtitles_without_cues_are_not_burned() -> None:
    graph = build_filtergraph(_two_scene_plan("shorts_vertical"))
    assert "[vcat]null[vout]" in graph and "ass=" not in graph


def test_exact_scene_keeps_its_full_source() -> None:
    graph = build_filtergraph(make_plan(scenes=((1500, 1500),)))
    assert "[0:v:0]trim=start=0:duration=1.500," in graph
    assert "tpad=stop_mode=clone:stop_duration=0.500,trim=duration=1.500," in graph


def test_argv_is_deterministic_and_orders_inputs_scenes_then_voices() -> None:
    plan = _two_scene_plan("shorts_vertical", subtitles=False)
    argv = build_argv(
        ffmpeg="/opt/ffmpeg",
        plan=plan,
        scene_paths=["/w/a.mp4", "/w/b.mp4"],
        voice_paths=["/w/s1.wav", "/w/s2.wav"],
        output="/w/out.mp4",
        threads=4,
    )
    assert argv == [
        "/opt/ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
        "-an", "-sn", "-dn", "-i", "/w/a.mp4",
        "-an", "-sn", "-dn", "-i", "/w/b.mp4",
        "-vn", "-sn", "-dn", "-i", "/w/s1.wav",
        "-vn", "-sn", "-dn", "-i", "/w/s2.wav",
        "-/filter_complex", "filtergraph.txt", "-filter_complex_threads", "4",
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "30000/1000", "-fps_mode", "cfr",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-threads", "4",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
        "-movflags", "+faststart", "-t", "2.000", "-f", "mp4", "/w/out.mp4",
    ]  # fmt: skip


def test_gop_is_passed_when_the_profile_sets_it() -> None:
    plan = make_plan()
    profile = plan.profile.model_copy(
        update={"video": plan.profile.video.model_copy(update={"gop_frames": 60})}
    )
    plan = plan.model_copy(update={"profile": profile})
    argv = build_argv(
        ffmpeg="f", plan=plan, scene_paths=["a"], voice_paths=["v"], output="o", threads=1
    )
    assert argv[argv.index("-g") + 1] == "60"


def test_argv_rejects_misaligned_inputs() -> None:
    plan = make_plan()
    with pytest.raises(ValueError):
        build_argv(ffmpeg="f", plan=plan, scene_paths=[], voice_paths=["v"], output="o", threads=1)
    with pytest.raises(ValueError):
        build_argv(
            ffmpeg="f", plan=plan, scene_paths=["a"], voice_paths=["v"], output="o", threads=0
        )


@pytest.mark.parametrize(
    ("ms", "text"), [(0, "0.000"), (7, "0.007"), (1500, "1.500"), (61001, "61.001")]
)
def test_seconds_is_exact(ms: int, text: str) -> None:
    assert seconds(ms) == text
