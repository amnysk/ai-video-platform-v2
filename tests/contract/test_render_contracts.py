"""Render の契約（ADR-0019）。

profile・plan・final_video の生成側と取り込み側を同じテストで照合する。
"""

from __future__ import annotations

import json
import uuid
from math import gcd
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.artifacts import (
    ARTIFACT_MODELS,
    build_final_video_artifact,
    parse_artifact,
    parse_final_video,
)
from contracts.render import (
    DEFAULT_RENDER_MAX_FREEZE_MS,
    DEFAULT_RENDER_PROFILE_ID,
    FINAL_VIDEO_MAX_BYTES,
    RENDER_PROFILES,
    RENDER_TEMPLATE_VERSION,
    FinalVideoArtifact,
    RenderPlan,
    RenderProfile,
    TimelinePolicy,
    get_render_profile,
)
from contracts.states import ArtifactType

SHA = "a" * 64


def _id() -> str:
    return str(uuid.uuid4())


def _ref() -> dict[str, Any]:
    return {"artifact_id": _id(), "sha256": SHA, "schema_version": "1.0"}


def _digest() -> dict[str, Any]:
    return {"artifact_id": _id(), "sha256": SHA}


def _scenes() -> list[dict[str, Any]]:
    return [
        {
            "scene_id": "sb1",
            "order": 1,
            "source_video": _digest(),
            "timeline_start_ms": 0,
            "timeline_duration_ms": 3000,
            "requested_duration_ms": 3000,
            "source_duration_ms": 3000,
            "reconciliation": {"mode": "exact", "trim_ms": 0, "freeze_ms": 0},
        },
        {
            "scene_id": "sb2",
            "order": 2,
            "source_video": _digest(),
            "timeline_start_ms": 3000,
            "timeline_duration_ms": 4000,
            "requested_duration_ms": 4000,
            "source_duration_ms": 5000,
            "reconciliation": {"mode": "trim", "trim_ms": 1000, "freeze_ms": 0},
        },
        {
            "scene_id": "sb3",
            "order": 3,
            "source_video": _digest(),
            "timeline_start_ms": 7000,
            "timeline_duration_ms": 3000,
            "requested_duration_ms": 3000,
            "source_duration_ms": 2000,
            "reconciliation": {"mode": "freeze_tail", "trim_ms": 0, "freeze_ms": 1000},
        },
    ]


def _voices() -> list[dict[str, Any]]:
    return [
        {
            "script_scene_id": "s1",
            "source_voice": _digest(),
            "start_ms": 0,
            "duration_ms": 2800,
            "storyboard_scene_ids": ["sb1"],
        },
        {
            "script_scene_id": "s2",
            "source_voice": _digest(),
            "start_ms": 3000,
            "duration_ms": 6500,
            "storyboard_scene_ids": ["sb2", "sb3"],
        },
    ]


def _cues() -> list[dict[str, Any]]:
    return [
        {"cue_index": 0, "script_scene_id": "s1", "char_start": 0, "char_end": 12,
         "start_ms": 0, "end_ms": 2800},
        {"cue_index": 1, "script_scene_id": "s2", "char_start": 0, "char_end": 10,
         "start_ms": 3000, "end_ms": 6000},
        {"cue_index": 2, "script_scene_id": "s2", "char_start": 10, "char_end": 20,
         "start_ms": 6000, "end_ms": 9500},
    ]  # fmt: skip


def _profile(profile_id: str = DEFAULT_RENDER_PROFILE_ID) -> dict[str, Any]:
    return get_render_profile(profile_id).model_dump(mode="json")


def _engine() -> dict[str, Any]:
    return {"engine": "engine-x", "version": "7.1.1", "binary_sha256": SHA}


def plan_payload(**overrides: Any) -> dict[str, Any]:
    return {
        "scenes": _scenes(),
        "voices": _voices(),
        "subtitle_cues": _cues(),
        "total_duration_ms": 10_000,
        "profile": _profile(),
        "policy": TimelinePolicy().model_dump(mode="json"),
        "engine": _engine(),
        **overrides,
    }


def _measured(profile: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    return {
        "width": profile["width"],
        "height": profile["height"],
        "duration_ms": 10_020,
        "fps_millis": profile["fps_millis"],
        "video_codec": "h264",
        "pix_fmt": "yuv420p",
        "audio_present": True,
        "audio_codec": "aac",
        "audio_sample_rate_hz": 48_000,
        "audio_channels": 2,
        **overrides,
    }


def final_kwargs(profile_id: str = DEFAULT_RENDER_PROFILE_ID, **overrides: Any) -> dict[str, Any]:
    profile = _profile(profile_id)
    return {
        "episode_id": "ep-1",
        "source_production_manifest": _ref(),
        "source_script": _ref(),
        "source_storyboard": _ref(),
        "render_profile": profile,
        "render_policy": TimelinePolicy().model_dump(mode="json"),
        "render_plan_sha256": "c" * 64,
        "render_engine": _engine(),
        "template_version": RENDER_TEMPLATE_VERSION,
        "media": {
            "object_key": f"media/ep-1/final_video/{'d' * 64}.mp4",
            "sha256": "d" * 64,
            "bytes": 3 * 1024 * 1024 * 1024,  # シーン素材の上限（25MB）を超えてよい
            "mime": "video/mp4",
        },
        "measured": _measured(profile),
        "total_duration_ms": 10_000,
        "timeline": _scenes(),
        "voice_placements": _voices(),
        "subtitle_cues": _cues(),
        "technical_qa": {
            "passed": True,
            "checks": [
                {"check": "decodable", "passed": True, "detail": "0 decode errors"},
                {"check": "resolution", "passed": True, "detail": ""},
            ],
        },
        **overrides,
    }


# --------------------------------------------------------------------------- profile


def test_both_builtin_profiles_are_valid_and_not_all_vertical() -> None:
    assert set(RENDER_PROFILES) >= {"shorts_vertical", "long_form_horizontal"}
    assert DEFAULT_RENDER_PROFILE_ID in RENDER_PROFILES
    ratios = set()
    for profile_id, profile in RENDER_PROFILES.items():
        assert profile.profile_id == profile_id
        RenderProfile.model_validate(profile.model_dump(mode="json"))
        ratios.add(profile.aspect_ratio)
    assert ratios == {"9:16", "16:9"}


@pytest.mark.parametrize(("width", "height"), [(1080, 1920), (1920, 1080), (1080, 1080)])
def test_aspect_ratio_is_derived_from_width_and_height(width: int, height: int) -> None:
    payload = _profile() | {"width": width, "height": height}
    profile = RenderProfile.model_validate(payload)
    g = gcd(width, height)
    assert profile.aspect_ratio == f"{width // g}:{height // g}"
    assert "aspect_ratio" not in profile.model_dump(mode="json")  # 保存しない（導出値）


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(KeyError):
        get_render_profile("no_such_profile")


def test_profile_rejects_odd_dimensions_and_inverted_limits() -> None:
    with pytest.raises(ValidationError):
        RenderProfile.model_validate(_profile() | {"width": 1081})
    limits = _profile()["limits"] | {"min_duration_ms": 10_000, "max_duration_ms": 5_000}
    with pytest.raises(ValidationError):
        RenderProfile.model_validate(_profile() | {"limits": limits})


def test_timeline_policy_defaults_come_from_the_contract_constant() -> None:
    policy = TimelinePolicy()
    assert policy.max_freeze_ms == DEFAULT_RENDER_MAX_FREEZE_MS
    assert policy.transition == "cut"
    assert policy.template_version == RENDER_TEMPLATE_VERSION


# --------------------------------------------------------------------------- plan


def test_valid_plan_round_trips_through_canonical_json() -> None:
    plan = RenderPlan.model_validate(plan_payload())
    dumped = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    assert RenderPlan.model_validate(json.loads(dumped)) == plan


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(total_duration_ms=9_999), id="sum_ne_total"),
        pytest.param(lambda p: p["scenes"][0].update(timeline_start_ms=5), id="not_from_zero"),
        pytest.param(lambda p: p["scenes"][1].update(timeline_start_ms=3001), id="gap"),
        pytest.param(lambda p: p["scenes"][1].update(order=3), id="order"),
        pytest.param(
            lambda p: p["scenes"][1]["reconciliation"].update(trim_ms=500), id="trim_mismatch"
        ),
        pytest.param(
            lambda p: p["scenes"][0].update(
                reconciliation={"mode": "exact", "trim_ms": 0, "freeze_ms": 10},
                timeline_duration_ms=3010,
            ),
            id="exact_with_freeze",
        ),
        pytest.param(lambda p: p["voices"][1].update(start_ms=2000), id="voice_overlap"),
        pytest.param(lambda p: p["voices"][1].update(duration_ms=7001), id="voice_past_total"),
        pytest.param(lambda p: p["subtitle_cues"][1].update(start_ms=2900), id="cue_overlap"),
        pytest.param(lambda p: p["subtitle_cues"][2].update(end_ms=9600), id="cue_outside_voice"),
        pytest.param(lambda p: p["subtitle_cues"][2].update(cue_index=5), id="cue_index"),
        pytest.param(lambda p: p["subtitle_cues"][2].update(char_start=5), id="char_order"),
        pytest.param(lambda p: p["subtitle_cues"][0].update(script_scene_id="s9"), id="cue_voice"),
    ],
)
def test_plan_rejects_inconsistent_timelines(mutate) -> None:
    payload = plan_payload()
    mutate(payload)
    with pytest.raises(ValidationError):
        RenderPlan.model_validate(payload)


def test_plan_and_final_video_carry_no_narration_text() -> None:
    for model in (RenderPlan, FinalVideoArtifact):
        dumped = json.dumps(model.model_json_schema())
        assert "narration" not in dumped
        assert '"text"' not in dumped


# --------------------------------------------------------------------------- final_video


@pytest.mark.parametrize("profile_id", ["shorts_vertical", "long_form_horizontal"])
def test_build_then_parse_round_trips(profile_id: str) -> None:
    payload = build_final_video_artifact(**final_kwargs(profile_id))
    assert payload["type"] == ArtifactType.FINAL_VIDEO.value
    parsed = parse_final_video(json.loads(json.dumps(payload, sort_keys=True)))
    assert isinstance(parsed, FinalVideoArtifact)
    assert parse_artifact(payload) == parsed
    assert ARTIFACT_MODELS[ArtifactType.FINAL_VIDEO] is FinalVideoArtifact


def test_final_media_cap_is_large_but_bounded() -> None:
    assert FINAL_VIDEO_MAX_BYTES >= 4 * 1024**3
    kwargs = final_kwargs()
    kwargs["media"] = kwargs["media"] | {"bytes": FINAL_VIDEO_MAX_BYTES + 1}
    with pytest.raises(ValidationError):
        build_final_video_artifact(**kwargs)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"technical_qa": {"passed": False, "checks": [{"check": "x", "passed": False,
                                                           "detail": ""}]}},
            id="qa_not_passed",
        ),
        pytest.param(
            {"technical_qa": {"passed": True, "checks": [{"check": "x", "passed": False,
                                                          "detail": ""}]}},
            id="qa_check_failed",
        ),
        pytest.param({"total_duration_ms": 9_000}, id="timeline_sum"),
        pytest.param({"media": {"object_key": "k", "sha256": SHA, "bytes": 1,
                                "mime": "video/webm"}}, id="mime"),
    ],
)  # fmt: skip
def test_final_video_rejects_internal_inconsistency(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        build_final_video_artifact(**final_kwargs(**overrides))


def test_final_video_measured_resolution_must_match_the_profile_snapshot() -> None:
    kwargs = final_kwargs("long_form_horizontal")
    kwargs["measured"] = kwargs["measured"] | {"width": 1080, "height": 1920}
    with pytest.raises(ValidationError):
        build_final_video_artifact(**kwargs)


def test_final_video_requires_audio_when_the_profile_requires_it() -> None:
    kwargs = final_kwargs()
    kwargs["measured"] = kwargs["measured"] | {
        "audio_present": False,
        "audio_codec": None,
        "audio_sample_rate_hz": None,
        "audio_channels": None,
    }
    with pytest.raises(ValidationError):
        build_final_video_artifact(**kwargs)


def test_unknown_schema_version_is_not_guessed() -> None:
    payload = build_final_video_artifact(**final_kwargs()) | {"schema_version": "2.0"}
    with pytest.raises(ValidationError):
        parse_final_video(payload)
