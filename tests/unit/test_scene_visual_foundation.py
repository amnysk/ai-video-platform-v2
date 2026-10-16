"""ADR-0035 の共有基盤: 映像対象の語彙、シーン単位の代替案 Artifact、実効シーンと指紋。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from contracts.artifacts import (
    SceneVisualOverrideArtifact,
    StoryboardScene,
    StoryboardVisualKind,
    VisualSubject,
    build_scene_visual_override_artifact,
    parse_artifact,
    parse_scene_visual_override_artifact,
)
from contracts.production_activities import (
    MAX_RECOVERY_COST_USD_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_EPISODE,
    MAX_SCENE_ALTERNATIVES_PER_SCENE,
)
from contracts.states import ArtifactType, RejectedInput
from domain.production.effective_scene import (
    SceneOverrideMismatchError,
    apply_override,
    scene_visual_fingerprint,
)

SHA = "a" * 64


def _scene(**overrides) -> StoryboardScene:
    fields = {
        "scene_id": "sb2",
        "order": 2,
        "script_scene_id": "s1",
        "start_ms": 4000,
        "duration_ms": 4000,
        "visual_kind": "generated",
        "visual_description": "Ieyasu stands on a low ridge in armor",
        "framing": "medium shot",
        "camera_movement": "slow push in",
        "transition_in": None,
    }
    fields.update(overrides)
    return StoryboardScene.model_validate(fields)


def _override(**overrides) -> dict:
    fields = {
        "episode_id": "ep-1",
        "source_storyboard": {
            "artifact_id": str(uuid.uuid4()),
            "sha256": SHA,
            "schema_version": "1.0",
        },
        "scene_id": "sb2",
        "revision": 1,
        "visual_kind": "broll",
        "visual_subject": "landscape",
        "visual_description": "The Sekigahara valley at dawn seen from a distant ridge",
        "framing": "wide establishing shot",
        "camera_movement": None,
        "rationale": "The battle site conveys the event; the name stays in narration.",
        "rejection_ids": [str(uuid.uuid4())],
        "planner": {
            "generator": "fake-planner",
            "generator_model": "fake",
            "generation_profile_id": "scene-alternative-v1",
        },
    }
    fields.update(overrides)
    return build_scene_visual_override_artifact(**fields)


# ------------------------------------------------------------------ 映像対象（Storyboard）


def test_old_storyboard_scene_without_visual_subject_still_parses() -> None:
    assert _scene().visual_subject is None


def test_storyboard_scene_accepts_a_visual_subject() -> None:
    scene = _scene(visual_subject="map")
    assert scene.visual_subject is VisualSubject.MAP


def test_visual_subject_vocabulary_is_closed() -> None:
    with pytest.raises(ValidationError):
        _scene(visual_subject="portrait_of_ieyasu")


def test_rejected_input_vocabulary() -> None:
    assert {r.value for r in RejectedInput} == {"image", "prompt", "unknown"}


# ------------------------------------------------------------------ 代替案 Artifact


def test_scene_visual_override_round_trips_through_parse_artifact() -> None:
    payload = _override()
    assert payload["type"] == ArtifactType.SCENE_VISUAL_OVERRIDE.value
    parsed = parse_artifact(payload)
    assert isinstance(parsed, SceneVisualOverrideArtifact)
    assert parsed == parse_scene_visual_override_artifact(payload)
    assert parsed.visual_subject is VisualSubject.LANDSCAPE


@pytest.mark.parametrize(
    ("field", "value"),
    [("revision", 0), ("rationale", ""), ("rejection_ids", []), ("visual_description", "")],
)
def test_scene_visual_override_rejects_incomplete_plans(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        _override(**{field: value})


# ------------------------------------------------------------------ 実効シーン


def test_apply_override_replaces_only_visual_fields() -> None:
    scene = _scene()
    override = parse_scene_visual_override_artifact(_override())
    effective = apply_override(scene, override)
    assert effective.visual_description == override.visual_description
    assert effective.visual_subject is VisualSubject.LANDSCAPE
    assert effective.visual_kind is StoryboardVisualKind.BROLL
    assert effective.framing == "wide establishing shot"
    assert effective.camera_movement is None
    # 時間割と台本の対応は変えない（音声・字幕・尺は override の対象外）
    for field in ("scene_id", "order", "script_scene_id", "start_ms", "duration_ms"):
        assert getattr(effective, field) == getattr(scene, field)


def test_apply_override_without_override_is_identity() -> None:
    scene = _scene()
    assert apply_override(scene, None) == scene


def test_apply_override_for_another_scene_is_refused() -> None:
    override = parse_scene_visual_override_artifact(_override(scene_id="sb3"))
    with pytest.raises(SceneOverrideMismatchError):
        apply_override(_scene(), override)


# ------------------------------------------------------------------ シーン単位の指紋


def test_fingerprint_ignores_position_on_the_timeline() -> None:
    assert scene_visual_fingerprint(_scene()) == scene_visual_fingerprint(
        _scene(order=5, start_ms=12000)
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("visual_description", "A map of the Tokaido"),
        ("visual_subject", "map"),
        ("visual_kind", "diagram"),
        ("framing", "wide"),
        ("camera_movement", "static"),
        ("transition_in", "fade"),
        ("duration_ms", 5000),
        ("script_scene_id", "s2"),
    ],
)
def test_fingerprint_changes_when_what_the_scene_shows_changes(field: str, value: object) -> None:
    assert scene_visual_fingerprint(_scene()) != scene_visual_fingerprint(_scene(**{field: value}))


def test_fingerprint_is_per_scene() -> None:
    """別シーンの内容は指紋に入らない（1シーンの差し替えが他シーンを無効化しない）。"""
    sb1 = _scene(scene_id="sb1", order=1, start_ms=0)
    assert scene_visual_fingerprint(sb1) != scene_visual_fingerprint(_scene())
    # 実効シーンの差し替えは差し替えたシーンの指紋だけを変える
    override = parse_scene_visual_override_artifact(_override())
    assert scene_visual_fingerprint(apply_override(_scene(), override)) != scene_visual_fingerprint(
        _scene()
    )


def test_fingerprint_of_a_scene_without_subject_is_stable_across_the_new_optional_field() -> None:
    """旧 storyboard（visual_subject 無し）の指紋は、任意項目の追加で変わらない。"""
    scene = _scene()
    payload = scene.model_dump(mode="json")
    payload.pop("visual_subject", None)
    assert scene_visual_fingerprint(StoryboardScene.model_validate(payload)) == (
        scene_visual_fingerprint(scene)
    )


# ------------------------------------------------------------------ 上限（INV-34 の定義元）


def test_recovery_limits_are_positive_and_scene_limit_fits_episode_limit() -> None:
    assert MAX_SCENE_ALTERNATIVES_PER_SCENE >= 1
    assert MAX_SCENE_ALTERNATIVES_PER_EPISODE >= MAX_SCENE_ALTERNATIVES_PER_SCENE
    assert MAX_RECOVERY_COST_USD_PER_EPISODE > 0
