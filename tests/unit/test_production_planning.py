"""production の作業一覧とマニフェスト（ADR-0017）。"""

from __future__ import annotations

import uuid

import pytest

from contracts.artifacts import parse_production_manifest
from domain.errors import ProductionInputInvalidError, ProductionInputMissingError
from domain.production.manifest import ArtifactRef, build_manifest, check_manifest_coverage
from domain.production.planning import plan_production
from tests.support.production import sample_script, sample_storyboard

EP = "ep-1"


def test_plan_has_image_and_video_per_storyboard_scene_and_voice_per_script_scene() -> None:
    plan = plan_production(sample_script(EP), sample_storyboard(EP))
    assert [i.scene_id for i in plan.images] == ["sb1", "sb2", "sb3", "sb4"]
    assert [(v.scene_id, v.requested_duration_ms) for v in plan.videos] == [
        ("sb1", 8000),
        ("sb2", 4000),
        ("sb3", 5000),
        ("sb4", 8000),
    ]
    assert [(v.script_scene_id, v.storyboard_scene_ids) for v in plan.voices] == [
        ("s1", ("sb1",)),
        ("s2", ("sb2", "sb3")),
        ("s3", ("sb4",)),
    ]


def test_plan_rejects_uncovered_script_scene() -> None:
    script = sample_script(EP)
    storyboard = sample_storyboard(EP)
    trimmed = script.model_copy(
        update={"scenes": (*script.scenes, script.scenes[0].model_copy(update={"id": "s4"}))}
    )
    with pytest.raises(ProductionInputInvalidError, match="s4"):
        plan_production(trimmed, storyboard)


def test_plan_rejects_unknown_script_reference() -> None:
    script = sample_script(EP)
    storyboard = sample_storyboard(EP)
    bad_scene = storyboard.scenes[0].model_copy(update={"script_scene_id": "s9"})
    bad = storyboard.model_copy(update={"scenes": (bad_scene, *storyboard.scenes[1:])})
    with pytest.raises(ProductionInputInvalidError, match="s9"):
        plan_production(script, bad)


def test_plan_rejects_episode_mismatch() -> None:
    with pytest.raises(ProductionInputInvalidError):
        plan_production(sample_script(EP), sample_storyboard("ep-2"))


def _ref(sha: str = "d" * 64) -> ArtifactRef:
    return ArtifactRef(artifact_id=str(uuid.uuid4()), sha256=sha)


def _refs(keys: list[str]) -> dict[str, ArtifactRef]:
    return {k: _ref() for k in keys}


def _manifest(**overrides):
    script, storyboard = sample_script(EP), sample_storyboard(EP)
    kwargs = {
        "episode_id": EP,
        "storyboard_ref": _ref("a" * 64),
        "script_ref": _ref("b" * 64),
        "storyboard": storyboard,
        "script": script,
        "images": _refs(["sb1", "sb2", "sb3", "sb4"]),
        "videos": _refs(["sb1", "sb2", "sb3", "sb4"]),
        "voices": _refs(["s1", "s2", "s3"]),
    }
    kwargs.update(overrides)
    return build_manifest(**kwargs), storyboard, script


def test_manifest_covers_storyboard_and_script_in_order() -> None:
    payload, storyboard, script = _manifest()
    manifest = parse_production_manifest(payload)
    assert [s.scene_id for s in manifest.scenes] == ["sb1", "sb2", "sb3", "sb4"]
    assert [v.script_scene_id for v in manifest.voices] == ["s1", "s2", "s3"]
    check_manifest_coverage(manifest, storyboard, script)


@pytest.mark.parametrize("field", ["images", "videos", "voices"])
def test_manifest_missing_media_is_needs_input(field: str) -> None:
    keys = {"images": ["sb1", "sb2", "sb3"], "videos": ["sb1", "sb2", "sb3"], "voices": ["s1"]}
    with pytest.raises(ProductionInputMissingError):
        _manifest(**{field: _refs(keys[field])})


def test_manifest_extra_media_is_invalid() -> None:
    with pytest.raises(ProductionInputInvalidError):
        _manifest(images=_refs(["sb1", "sb2", "sb3", "sb4", "sb5"]))


def test_coverage_check_rejects_a_manifest_for_another_storyboard() -> None:
    payload, storyboard, script = _manifest()
    manifest = parse_production_manifest(payload)
    shorter = manifest.model_copy(update={"scenes": manifest.scenes[:3]})
    with pytest.raises(ProductionInputInvalidError):
        check_manifest_coverage(shorter, storyboard, script)
