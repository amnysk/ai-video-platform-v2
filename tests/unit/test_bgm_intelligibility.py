"""ナレーション優先: 発話中の声と BGM の差の評価と、計測用 filtergraph（ADR-0083 §5、INV-135）。"""

from __future__ import annotations

import pytest

from contracts.music_render import SPEECH_MARGIN_MIN_CENTI_LU
from domain.music.intelligibility import SceneSpeechLevels, evaluate_speech_intelligibility
from domain.render.bgm import resolve_bgm
from infrastructure.render.ffmpeg_graph import (
    build_bgm_stem_filtergraph,
    build_filtergraph,
    build_voice_stem_filtergraph,
)
from tests.support.bgm_catalog import NOW, make_track
from tests.support.bgm_render import bgm_plan, cue, file_shas, sheet

T = SPEECH_MARGIN_MIN_CENTI_LU


def _lv(scene: str, voice, music) -> SceneSpeechLevels:
    return SceneSpeechLevels(scene, voice, music)


def test_margin_is_voice_minus_music_and_the_smallest_one_decides() -> None:
    result = evaluate_speech_intelligibility(
        [_lv("s1", -1600, -1600 - T - 500), _lv("s2", -1700, -1700 - T)]
    )
    assert [s.margin_centi_lu for s in result.scenes] == [T + 500, T]
    assert result.min_margin_centi_lu == T and result.passed  # 閾値ちょうどは合格


def test_one_scene_below_the_threshold_fails_the_whole_episode() -> None:
    result = evaluate_speech_intelligibility(
        [_lv("s1", -1600, -3600), _lv("s2", -1600, -1600 - T + 1)]
    )
    assert not result.passed and result.min_margin_centi_lu == T - 1


def test_a_scene_without_music_is_not_a_failure_but_unmeasured_voice_is_not_a_pass() -> None:
    assert evaluate_speech_intelligibility([_lv("s1", -1600, None)]).passed
    assert not evaluate_speech_intelligibility([_lv("s1", None, -3000)]).passed
    assert not evaluate_speech_intelligibility([_lv("s1", None, None)]).passed


def test_no_levels_is_an_error_not_a_pass() -> None:
    with pytest.raises(ValueError):
        evaluate_speech_intelligibility([])


def _mix():
    track = make_track("bgm-approved", "a")
    res = resolve_bgm(
        sheet(cue(), cue(scene_id="s2", start_ms=2000, end_ms=4000, source_offset_ms=2000)),
        (track,), plan=bgm_plan(), now=NOW, file_sha256_by_track=file_shas(track),
    )  # fmt: skip
    assert res.plan is not None
    return res.plan


def test_the_music_stem_reuses_the_real_bgm_chain_and_leaves_nothing_dangling() -> None:
    plan, mix = bgm_plan(), _mix()
    stem = build_bgm_stem_filtergraph(plan, mix)
    real = build_filtergraph(plan, mix)
    # 本番の cue ごとの鎖（fade・level・遅延・ducking）が一字一句同じ部分を含む（入力番号だけ違う）
    chain = [ln for ln in real.splitlines() if ln.startswith("[b0][sc0]sidechaincompress")]
    assert chain and all(ln in stem for ln in chain)
    assert stem.rstrip().endswith("[aout]")
    assert "[v0]" not in stem and "concat" not in stem  # 映像を含まない
    # 入力番号は 0 起点（ナレーション → cue）
    assert "[0:a:0]" in stem and "[2:a:0]" in stem and "[4:a:0]" not in stem


def test_the_music_stem_has_no_unconsumed_branch_that_can_deadlock_ffmpeg() -> None:
    """声の mix の枝は sidechain の分だけ。誰も引かない枝（anullsink）を残すと ffmpeg が固まる。

    実測（Episode 48f4eddb、固定版 7.1.1、無負荷、同一入力）: 旧 graph は 150 回中 14 回・7 回が
    300 秒の timeout まで戻らず、枝を除いた graph は 200 回中 0 回。
    """
    stem = build_bgm_stem_filtergraph(bgm_plan(), _mix())
    assert "anullsink" not in stem and "[vmix]" not in stem
    split = next(ln for ln in stem.splitlines() if "asplit=" in ln)
    n = int(split.split("asplit=")[1].split("[")[0])
    outputs = split.split("asplit=")[1].count("[")
    assert n == outputs == stem.count("sidechaincompress")  # 枝は全部 sidechain が使う


def test_the_voice_stem_is_the_narration_bus_of_the_bgm_less_render() -> None:
    plan = bgm_plan()
    voice = build_voice_stem_filtergraph(plan)
    plain = build_filtergraph(plan)
    bus = [ln for ln in plain.splitlines() if ln.startswith("[abase][a0][a1]amix")][0]
    assert bus.replace("[aout]", "") in voice.replace("[aout]", "")
