"""BGM を混ぜる filtergraph / argv（ADR-0077 §4、INV-113）。純粋関数のスナップショット。"""

from __future__ import annotations

import pytest

from domain.render.bgm import resolve_bgm
from infrastructure.render.ffmpeg_graph import build_argv, build_filtergraph
from tests.support.bgm_catalog import NOW, make_track
from tests.support.bgm_render import bgm_plan, cue, file_shas, sheet

TRACK = make_track("bgm-approved", "a")

EXPECTED_TAIL = """\
[abase][a0][a1]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,atrim=duration=4.000,asplit=2[vmix][sc0];
[4:a:0]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=start=0:duration=2.000,asetpts=PTS-STARTPTS,volume=-18dB,afade=t=in:st=0:d=0.200,afade=t=out:st=1.700:d=0.300,adelay=delays=0:all=1,apad=whole_dur=4.000[b0];
[5:a:0]aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,atrim=start=0:duration=2.000,asetpts=PTS-STARTPTS,volume=-18dB,afade=t=out:st=1.700:d=0.300,adelay=delays=2000:all=1,apad=whole_dur=4.000[d1];
[b0][sc0]sidechaincompress=threshold=0.03:ratio=9:attack=20:release=300:makeup=1[d0];
[vmix][d0][d1]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.9:level=0,atrim=duration=4.000[aout]
"""  # noqa: E501


def _mix(*cues):
    res = resolve_bgm(
        sheet(*cues),
        (TRACK,),
        plan=bgm_plan(),
        now=NOW,
        file_sha256_by_track=file_shas(TRACK),
    )
    assert res.plan is not None
    return res.plan


def _two_cues():
    return _mix(
        cue(), cue(ducking_policy="none", scene_id="s2", start_ms=2000, end_ms=4000, fade_in_ms=0)
    )


def test_bgm_graph_tail_snapshot_and_video_part_is_unchanged() -> None:
    plan = bgm_plan()
    with_bgm = build_filtergraph(plan, _two_cues())
    assert with_bgm.endswith(EXPECTED_TAIL)
    without = build_filtergraph(plan)
    shared = without.split("[abase][a0]")[0]
    assert with_bgm.startswith(shared)


def test_duck_cue_uses_the_narration_bus_as_sidechain_and_none_does_not() -> None:
    graph = build_filtergraph(bgm_plan(), _mix(cue(ducking_policy="none")))
    assert "sidechaincompress" not in graph and "asplit" not in graph
    assert "[vmix]" in graph


def test_zero_length_fades_emit_no_afade() -> None:
    graph = build_filtergraph(bgm_plan(), _mix(cue(fade_in_ms=0, fade_out_ms=0)))
    assert "afade" not in graph


def test_argv_adds_one_looping_input_per_cue_after_the_voices() -> None:
    plan = bgm_plan()
    mix = _two_cues()
    argv = build_argv(
        ffmpeg="/ff",
        plan=plan,
        scene_paths=["/s0", "/s1"],
        voice_paths=["/v0", "/v1"],
        output="/o.mp4",
        threads=2,
        bgm=mix,
        bgm_paths=["/m", "/m"],
    )
    tail = argv[argv.index("/v1") + 1 : argv.index("-/filter_complex")]
    assert tail == ["-stream_loop", "-1", "-vn", "-sn", "-dn", "-i", "/m"] * 2


def test_argv_refuses_misaligned_bgm_inputs() -> None:
    kwargs = dict(
        ffmpeg="/ff", plan=bgm_plan(), scene_paths=["/s0", "/s1"],
        voice_paths=["/v0", "/v1"], output="/o", threads=1,
    )  # fmt: skip
    with pytest.raises(ValueError):
        build_argv(**kwargs, bgm=_two_cues(), bgm_paths=["/m"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        build_argv(**kwargs, bgm_paths=["/m"])  # type: ignore[arg-type]
