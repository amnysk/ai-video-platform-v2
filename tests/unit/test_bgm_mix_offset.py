"""曲ごとの ``mix_gain_offset_db`` と CK61 の再現（ADR-0087 §6、INV-154）。

所有者が試聴して承認したのは CK61（loudness -14.5 LUFS）の「+6 dB」版。既定の役割での配置は
[-15, -18, -18, -18, -16] で、+6 すると [-9, -12, -12, -12, -10]。``resolve_bgm`` は上限
``BGM_NARRATION_PRIORITY_MAX_DB`` (-12) に丸めるので全 cue が -12。
``mix_gain_offset_db=+6`` を持つ曲を配置関数で置いた結果が、試聴の経路
（配置 -> ``shift_levels(+6)``）と**同一の混合計画**であることを示す。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from contracts.music_render import BGM_NARRATION_PRIORITY_MAX_DB
from domain.music.placement import DEFAULT_POLICY, SceneRole, gain_db_for, plan_cue_sheet
from domain.render.bgm import resolve_bgm
from tests.support.bgm_catalog import NOW, make_track
from tests.support.bgm_render import file_shas
from tests.support.render_plans import make_plan

REPO = Path(__file__).resolve().parents[2]


def _preview_tool():
    spec = importlib.util.spec_from_file_location(
        "bgm_preview_cli_offset", REPO / "scripts" / "bgm_preview.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _five_scene_plan():
    base = make_plan(
        scenes=((4000, 4000),) * 5,
        voices=tuple((i * 4000 + 200, 3000) for i in range(5)),
        subtitles=False,
    )
    voices = tuple(
        v.model_copy(update={"storyboard_scene_ids": (f"sb{i}",)})
        for i, v in enumerate(base.voices, start=1)
    )
    return base.model_copy(update={"voices": voices})


def _ck61(**kw):
    return make_track("bgm-cc0-ambient-ck61", "a", loudness_lufs=-14.5, **kw)


def test_default_roles_without_an_offset_give_the_documented_levels() -> None:
    sheet = plan_cue_sheet(_five_scene_plan(), _ck61()).cue_sheet
    assert sheet is not None
    assert [c.target_relative_level_db for c in sheet.cues] == [-15, -18, -18, -18, -16]


def test_ck61_with_a_plus_six_offset_is_the_same_mix_plan_as_the_approved_preview() -> None:
    plan = _five_scene_plan()
    plain, tuned = _ck61(), _ck61(mix_gain_offset_db=6)

    # 試聴の経路: オフセット無しで配置 -> すべての cue を +6 dB ずらす（scripts/bgm_preview.py）
    base_sheet = plan_cue_sheet(plan, plain).cue_sheet
    assert base_sheet is not None
    preview_sheet = _preview_tool().shift_levels(base_sheet, 6)
    # 新しい経路: 曲が +6 を持つ
    new_sheet = plan_cue_sheet(plan, tuned).cue_sheet
    assert new_sheet is not None

    def mix(sheet, track):
        res = resolve_bgm(
            sheet, (track,), plan=plan, now=NOW, file_sha256_by_track=file_shas(track)
        )
        assert res.applied, res.rejections
        assert res.plan is not None
        return res.plan

    preview_mix, new_mix = mix(preview_sheet, plain), mix(new_sheet, tuned)
    assert [c.level_db for c in new_mix.cues] == [BGM_NARRATION_PRIORITY_MAX_DB] * 5
    assert new_mix.identity_payload() == preview_mix.identity_payload()


def test_the_offset_and_the_fallback_attenuation_add_and_the_cap_still_holds() -> None:
    track = _ck61(mix_gain_offset_db=6)
    # intro は素の gain -15。+6 -> -9 を上限 -12 が抑える。減衰 -3 を足すと -15 + 6 - 3 = -12
    assert gain_db_for(track, SceneRole.INTRO, DEFAULT_POLICY) == -12
    assert gain_db_for(track, SceneRole.INTRO, DEFAULT_POLICY, extra_offset_db=-3) == -12
    assert gain_db_for(track, SceneRole.INTRO, DEFAULT_POLICY, extra_offset_db=-9) == -18
    # 持ち上げても声より BGM_NARRATION_PRIORITY_MAX_DB を超えて大きくならない
    huge = _ck61(mix_gain_offset_db=12)
    assert gain_db_for(huge, SceneRole.CLIMAX, DEFAULT_POLICY) == BGM_NARRATION_PRIORITY_MAX_DB


def test_a_negative_offset_and_the_extra_attenuation_lower_every_cue_and_stay_in_bounds() -> None:
    plan = _five_scene_plan()
    sheet = plan_cue_sheet(plan, _ck61(mix_gain_offset_db=-4), extra_gain_offset_db=-3).cue_sheet
    assert sheet is not None
    assert [c.target_relative_level_db for c in sheet.cues] == [-22, -25, -25, -25, -23]
