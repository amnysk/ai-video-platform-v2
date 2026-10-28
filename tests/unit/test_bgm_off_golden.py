"""BGM が OFF（設定無効・cue sheet 無し）のとき、描画の同一性と ffmpeg が導入前と同じ（INV-113）。

期待値は BGM 結線を入れる**前**のコミット（6d14c30 = 本番 303f33e + v3 wave 1）で、同じ入力から
実測した値を固定したもの。実装から導いた値ではないので、結線側の変更でここが動いたら
実装を直す（テストを合わせない）。
"""

from __future__ import annotations

import hashlib

from contracts.render import get_render_profile
from domain.render.identity import render_input_hash
from infrastructure.render.ffmpeg_graph import build_argv, build_filtergraph
from tests.support.render_plans import make_plan
from tests.unit.test_render_identity import BASE

#: 6d14c30 で ``render_input_hash(**BASE)`` / ``engine_threads=4`` を実測した値
GOLDEN_INPUT_HASH = "ff4573e2a36f8731ef76e474337e3a6555de0ec81a7fdd71cbf9decc8b3e9794"
GOLDEN_INPUT_HASH_THREADS4 = "c10f3109f18d8103204a7beedc2c1420b7b4a0a2673a4857c182ef8d14b7371d"
#: 同じ plan（2 シーン・2 ナレーション・字幕 2 枚）の filtergraph の sha256（6d14c30 で実測）
GOLDEN_GRAPH_SHA256 = {
    "shorts_vertical": "a84470aa7f3f98ad2c45d9c0d8b1992cc8fca25cd2de72b69cb8626f22015d07",
    "long_form_horizontal": "4a0af276d3da6b8de6ac58a329317af678039a25f0d26aa97482132bec1794e7",
}
GOLDEN_ARGV = [
    "/ff", "-hide_banner", "-nostdin", "-nostats", "-loglevel", "error", "-y",
    "-an", "-sn", "-dn", "-i", "/s0", "-an", "-sn", "-dn", "-i", "/s1",
    "-vn", "-sn", "-dn", "-i", "/v0", "-vn", "-sn", "-dn", "-i", "/v1",
    "-/filter_complex", "filtergraph.txt", "-filter_complex_threads", "4",
    "-map", "[vout]", "-map", "[aout]",
    "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
    "-r", "30000/1000", "-fps_mode", "cfr", "-c:a", "aac", "-b:a", "192k",
    "-ar", "48000", "-ac", "2", "-threads", "4", "-map_metadata", "-1", "-map_chapters", "-1",
    "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact",
    "-movflags", "+faststart", "-t", "2.000", "-f", "mp4", "/o.mp4",
]  # fmt: skip


def _plan(profile: str):
    return make_plan(
        profile=profile,
        scenes=((1200, 1000), (800, 1000)),
        voices=((0, 900), (1100, 700)),
        cues=((0, 0, 900), (1, 1100, 1800)),
    )


def test_input_hash_without_bgm_equals_the_pre_bgm_value() -> None:
    assert render_input_hash(**BASE) == GOLDEN_INPUT_HASH
    assert render_input_hash(**BASE, engine_threads=4) == GOLDEN_INPUT_HASH_THREADS4
    assert get_render_profile("shorts_vertical") is BASE["profile"]


def test_filtergraph_without_bgm_equals_the_pre_bgm_value() -> None:
    for profile, expected in GOLDEN_GRAPH_SHA256.items():
        graph = build_filtergraph(_plan(profile))
        assert hashlib.sha256(graph.encode()).hexdigest() == expected


def test_argv_without_bgm_equals_the_pre_bgm_value() -> None:
    argv = build_argv(
        ffmpeg="/ff",
        plan=_plan("shorts_vertical"),
        scene_paths=["/s0", "/s1"],
        voice_paths=["/v0", "/v1"],
        output="/o.mp4",
        threads=4,
    )
    assert argv == GOLDEN_ARGV
