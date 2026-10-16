"""描画計画の組み立て（ADR-0019 §2）。"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest

from contracts.render import TimelinePolicy, get_render_profile
from domain.errors import (
    DurationReconciliationError,
    RenderInputIntegrityError,
    RenderInputMissingError,
)
from domain.production.manifest import ArtifactRef
from domain.render.identity import render_plan_sha256
from domain.render.plan import Pinned
from tests.support.render_fixtures import ENGINE, render_inputs


def test_plan_covers_every_scene_voice_and_cue() -> None:
    inputs = render_inputs()
    plan = inputs.plan()
    assert [s.scene_id for s in plan.scenes] == ["sb1", "sb2", "sb3", "sb4"]
    assert [s.source_video.sha256 for s in plan.scenes] == [
        inputs.videos[sid].ref.sha256 for sid in ("sb1", "sb2", "sb3", "sb4")
    ]
    assert [v.script_scene_id for v in plan.voices] == ["s1", "s2", "s3"]
    assert plan.total_duration_ms == 25000
    assert len(plan.subtitle_cues) == 3
    assert plan.engine == ENGINE
    dumped = plan.model_dump_json()
    assert "焦げ跡" not in dumped  # ナレーション本文を複製しない


def test_plan_is_deterministic_and_hash_changes_with_profile() -> None:
    inputs = render_inputs()
    assert render_plan_sha256(inputs.plan()) == render_plan_sha256(inputs.plan())
    horizontal = inputs.plan(profile=get_render_profile("long_form_horizontal"))
    assert render_plan_sha256(horizontal) != render_plan_sha256(inputs.plan())


def test_subtitles_disabled_profile() -> None:
    profile = get_render_profile("shorts_vertical")
    profile = profile.model_copy(
        update={"subtitles": profile.subtitles.model_copy(update={"enabled": False})}
    )
    assert render_inputs().plan(profile=profile).subtitle_cues == ()


def test_reconciliation_error_propagates() -> None:
    inputs = render_inputs(video_durations={"sb2": 1000})
    with pytest.raises(DurationReconciliationError):
        inputs.plan(policy=TimelinePolicy(max_freeze_ms=2000))


def test_missing_video_is_missing_input() -> None:
    inputs = render_inputs()
    videos = dict(inputs.videos)
    del videos["sb2"]
    with pytest.raises(RenderInputMissingError):
        replace(inputs, videos=videos).plan()


def test_extra_voice_is_integrity_error() -> None:
    inputs = render_inputs()
    voices = dict(inputs.voices)
    voices["s9"] = voices["s1"]
    with pytest.raises(RenderInputIntegrityError):
        replace(inputs, voices=voices).plan()


def _other_ref(ref: ArtifactRef, *, sha: bool) -> ArtifactRef:
    if sha:
        return ArtifactRef(ref.artifact_id, "0" * 64)
    return ArtifactRef(str(uuid.uuid4()), ref.sha256)


@pytest.mark.parametrize("sha", [True, False])
@pytest.mark.parametrize("target", ["video", "voice", "script", "storyboard"])
def test_ref_mismatch_with_manifest_is_integrity_error(target: str, sha: bool) -> None:
    inputs = render_inputs()
    if target == "video":
        p = inputs.videos["sb3"]
        changed = replace(
            inputs, videos={**inputs.videos, "sb3": Pinned(p.artifact, _other_ref(p.ref, sha=sha))}
        )
    elif target == "voice":
        v = inputs.voices["s2"]
        changed = replace(
            inputs, voices={**inputs.voices, "s2": Pinned(v.artifact, _other_ref(v.ref, sha=sha))}
        )
    elif target == "script":
        changed = replace(
            inputs, script=Pinned(inputs.script.artifact, _other_ref(inputs.script.ref, sha=sha))
        )
    else:
        changed = replace(
            inputs,
            storyboard=Pinned(
                inputs.storyboard.artifact, _other_ref(inputs.storyboard.ref, sha=sha)
            ),
        )
    with pytest.raises(RenderInputIntegrityError):
        changed.plan()


def test_artifact_from_another_storyboard_is_integrity_error() -> None:
    inputs = render_inputs()
    other = render_inputs()
    # 別の入力一式のシーン動画をマニフェストの参照ごと差し替えることはできない（sha が違う）。
    # 参照だけ合わせて本体の source_storyboard が違う場合も弾く。
    foreign = other.videos["sb1"]
    changed = replace(
        inputs,
        videos={**inputs.videos, "sb1": Pinned(foreign.artifact, inputs.videos["sb1"].ref)},
    )
    with pytest.raises(RenderInputIntegrityError):
        changed.plan()


def test_video_for_wrong_scene_is_integrity_error() -> None:
    inputs = render_inputs()
    changed = replace(
        inputs,
        videos={
            **inputs.videos,
            "sb1": Pinned(inputs.videos["sb2"].artifact, inputs.videos["sb1"].ref),
        },
    )
    with pytest.raises(RenderInputIntegrityError):
        changed.plan()


@pytest.mark.parametrize(
    ("field", "value"), [("max_duration_ms", 24_999), ("min_duration_ms", 25_001)]
)
def test_total_outside_profile_limits_fails_before_rendering(field: str, value: int) -> None:
    profile = get_render_profile("shorts_vertical")
    limits = profile.limits.model_copy(update={field: value})
    if field == "min_duration_ms":
        limits = limits.model_copy(update={"max_duration_ms": 180_000})
    profile = profile.model_copy(update={"limits": limits})
    with pytest.raises(DurationReconciliationError):
        render_inputs().plan(profile=profile)


def test_total_on_profile_limits_is_accepted() -> None:
    profile = get_render_profile("shorts_vertical")
    limits = profile.limits.model_copy(
        update={"min_duration_ms": 25_000, "max_duration_ms": 25_000}
    )
    assert (
        render_inputs()
        .plan(profile=profile.model_copy(update={"limits": limits}))
        .total_duration_ms
        == 25_000
    )
