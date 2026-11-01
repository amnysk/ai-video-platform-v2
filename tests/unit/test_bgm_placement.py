"""BGM の選曲と scene への配置（ADR-0083 §3 / §4、INV-134）。純粋関数。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from contracts.music import BgmTrack
from contracts.music_render import BGM_NARRATION_PRIORITY_MAX_DB
from domain.music.placement import (
    PlacementPolicy,
    PlacementRefusal,
    SceneRole,
    choose_track,
    gain_db_for,
    plan_cue_sheet,
    scene_windows,
)
from domain.render.bgm import resolve_bgm
from tests.support.bgm_catalog import NOW, make_track
from tests.support.bgm_render import file_shas, placement_plan

LOUD = {"loudness_lufs": -18.0}


def _track(**kw) -> BgmTrack:
    return make_track("bgm-approved", "a", **{**LOUD, **kw})


def test_scene_windows_follow_the_storyboard_scenes_each_voice_covers() -> None:
    windows = scene_windows(placement_plan())
    assert [(w.scene_id, w.start_ms, w.end_ms) for w in windows] == [
        ("s1", 0, 8000),
        ("s2", 8000, 12000),
        ("s3", 12000, 20000),
    ]


def test_the_same_inputs_give_the_same_cue_sheet() -> None:
    plan, track = placement_plan(), _track()
    first = plan_cue_sheet(plan, track).cue_sheet
    second = plan_cue_sheet(plan, track).cue_sheet
    assert first is not None and second is not None and first == second
    assert first.model_dump_json() == second.model_dump_json()


def test_default_roles_open_with_intro_and_close_with_outro_and_the_sheet_is_resolvable() -> None:
    plan, track = placement_plan(), _track()
    sheet = plan_cue_sheet(plan, track).cue_sheet
    assert sheet is not None
    assert [c.scene_id for c in sheet.cues] == ["s1", "s2", "s3"]
    first, last = sheet.cues[0], sheet.cues[-1]
    assert first.start_ms == 0 and first.fade_in_ms == 1500
    assert last.end_ms == plan.total_duration_ms and last.fade_out_ms == 2500
    # 描画側の再検査（ライセンス・時間軸・scene）を通る = 配置が契約に適合している
    res = resolve_bgm(sheet, (track,), plan=plan, now=NOW, file_sha256_by_track=file_shas(track))
    assert res.applied, res.rejections


def test_one_continuous_run_keeps_the_same_music_position_across_scene_boundaries() -> None:
    """scene をまたいで曲が頭から鳴り直さない（source_offset_ms が区間の頭からの経過になる）。"""
    sheet = plan_cue_sheet(placement_plan(), _track()).cue_sheet
    assert sheet is not None
    run_start = sheet.cues[0].start_ms
    for cue in sheet.cues:
        assert cue.source_offset_ms == cue.start_ms - run_start
    # 隣り合う cue は境目をまたいで重なる（クロスフェード）
    for a, b in zip(sheet.cues, sheet.cues[1:], strict=False):
        assert b.start_ms < a.end_ms
        assert a.end_ms - b.start_ms == PlacementPolicy().crossfade_ms


def test_a_silent_scene_has_no_music_and_the_music_restarts_after_it() -> None:
    sheet = plan_cue_sheet(placement_plan(), _track(), roles={"s2": SceneRole.SILENT}).cue_sheet
    assert sheet is not None
    assert [c.scene_id for c in sheet.cues] == ["s1", "s3"]
    s1, s3 = sheet.cues
    assert s1.end_ms == 8000 and s3.start_ms == 12000  # 無音 scene (8000-12000) に cue が掛からない
    assert s1.fade_out_ms == PlacementPolicy().crossfade_ms  # 無音の手前でフェードアウト
    assert s3.source_offset_ms == 0  # 新しい区間なので曲頭から


def test_roles_set_the_level_and_climax_is_louder_than_body_but_never_above_the_cap() -> None:
    track, policy = _track(), PlacementPolicy()
    gains = {role: gain_db_for(track, role, policy) for role in policy.role_relative_db}
    assert gains[SceneRole.CLIMAX] > gains[SceneRole.BODY]
    assert all(g <= BGM_NARRATION_PRIORITY_MAX_DB for g in gains.values())
    # 曲が大きいほど gain は下がる（曲の実測ラウドネスを補正している）
    hot = _track(loudness_lufs=-10.0)
    assert gain_db_for(hot, SceneRole.BODY, policy) < gains[SceneRole.BODY]


def test_a_very_quiet_track_is_clamped_to_the_narration_priority_cap() -> None:
    quiet = _track(loudness_lufs=-60.0)
    assert gain_db_for(quiet, SceneRole.CLIMAX, PlacementPolicy()) == BGM_NARRATION_PRIORITY_MAX_DB


@pytest.mark.parametrize(
    ("track_kw", "roles", "refusal"),
    [
        ({"loudness_lufs": None}, None, PlacementRefusal.TRACK_LOUDNESS_UNKNOWN),
        (
            {"license_status": "unverified", "license_evidence_sha256": None,
             "license_checked_at": None},
            None,
            PlacementRefusal.TRACK_NOT_APPROVED,
        ),
        ({}, {"s1": SceneRole.SILENT, "s2": SceneRole.SILENT, "s3": SceneRole.SILENT},
         PlacementRefusal.ALL_SCENES_SILENT),
        ({}, {"s9": SceneRole.BODY}, PlacementRefusal.UNKNOWN_SCENE_ROLE),
    ],
)  # fmt: skip
def test_placement_refuses_instead_of_guessing(track_kw, roles, refusal) -> None:
    result = plan_cue_sheet(placement_plan(), _track(**track_kw), roles=roles)
    assert result.cue_sheet is None and result.refusal is refusal


def test_cues_carry_the_catalog_facts_verbatim_including_the_credit_text() -> None:
    text = 'Music: "Fixture Song" by Fixture Artist (CC BY 4.0)'
    track = _track(attribution_required=True, exact_attribution_text=text)
    sheet = plan_cue_sheet(placement_plan(), track).cue_sheet
    assert sheet is not None
    assert {c.credits_text for c in sheet.cues} == {text}
    assert {c.source_sha256 for c in sheet.cues} == {track.file_sha256}
    assert {c.license_evidence_hash for c in sheet.cues} == {track.license_evidence_sha256}


def test_scenes_shorter_than_the_fades_still_give_a_valid_sheet() -> None:
    policy = PlacementPolicy(crossfade_ms=6000, opening_fade_in_ms=6000, closing_fade_out_ms=6000)
    sheet = plan_cue_sheet(placement_plan(), _track(), policy=policy).cue_sheet
    assert sheet is not None  # MusicCue の契約（fade の合計が cue に収まる）を満たす
    for cue in sheet.cues:
        assert cue.fade_in_ms + cue.fade_out_ms <= cue.end_ms - cue.start_ms


# --------------------------------------------------------------------------- 選曲


def _catalog() -> tuple[BgmTrack, ...]:
    return (
        make_track("bgm-b-high", "b", energy="high", **LOUD),
        make_track("bgm-a-low", "a", energy="low", **LOUD),
        make_track("bgm-c-low", "c", energy="low", mood_tags=("upbeat",), **LOUD),
        make_track("bgm-x-revoked", "d", license_status="revoked", **LOUD),
    )


def _choose_id(catalog, **kw) -> str | None:
    track = _choose(catalog, **kw)
    return None if track is None else track.track_id


def _choose(catalog, **kw):
    args = {
        "mood": "mysterious",
        "energy": "low",
        "fmt": "shorts",
        "platform": "youtube",
        "duration_ms": 40_000,
        "now": NOW,
        "recent_usage": {},
    }
    args.update(kw)
    return choose_track(catalog, **args)


def test_choice_is_deterministic_and_prefers_mood_then_less_used_then_energy() -> None:
    catalog = _catalog()
    assert _choose_id(catalog) == "bgm-a-low"
    assert _choose_id(tuple(reversed(catalog))) == "bgm-a-low"  # 入力の並びに依らない
    # 直近でよく使った曲は避ける（mood が同じなら）
    assert _choose_id(catalog, recent_usage={"bgm-a-low": 3}) == "bgm-b-high"
    # 同順位の中では energy が合う方
    assert _choose_id(catalog, energy="high", recent_usage={}) == "bgm-b-high"


def test_choice_never_returns_a_track_that_fails_a_license_fact() -> None:
    only_bad = (
        make_track("bgm-x-revoked", "d", license_status="revoked", **LOUD),
        make_track("bgm-y-expired", "e", valid_until=NOW - timedelta(seconds=1), **LOUD),
    )
    assert _choose(only_bad) is None
