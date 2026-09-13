"""StoryboardArtifact スキーマの契約（INV-10 / ADR-0015）。生成側と取り込み側を突き合わせる。"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.artifacts import (
    ARTIFACT_MODELS,
    STORYBOARD_ARTIFACT_SCHEMA_VERSION,
    STORYBOARD_MAX_SCENES,
    StoryboardArtifact,
    StoryboardVisualKind,
    build_storyboard_artifact,
    parse_artifact,
    parse_storyboard_artifact,
)
from contracts.states import ArtifactType

_SOURCE = {"artifact_id": str(uuid.UUID(int=1)), "sha256": "a" * 64, "schema_version": "1.0"}
_METADATA = {
    "generator": "storyboard-generator",
    "generator_model": "model-x",
    "generation_spec_id": "spec@abc/sha256:0123456789abcdef",
}


def _scenes(durations: list[int]) -> list[dict[str, Any]]:
    scenes = []
    start = 0
    for index, duration in enumerate(durations, start=1):
        scenes.append(
            {
                "scene_id": f"sb{index}",
                "order": index,
                "script_scene_id": f"s{index}",
                "start_ms": start,
                "duration_ms": duration,
                "visual_kind": "broll",
                "visual_description": f"映像{index}",
                "framing": "wide",
                "camera_movement": None,
                "transition_in": None,
            }
        )
        start += duration
    return scenes


def _payload(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "episode_id": "ep-1",
        "source_script": dict(_SOURCE),
        "scenes": _scenes([6_000, 6_000, 6_000]),
        "total_duration_ms": 18_000,
        "metadata": dict(_METADATA),
    }
    kwargs.update(overrides)
    return build_storyboard_artifact(**kwargs)


def test_generated_storyboard_matches_the_documented_shape() -> None:
    payload = _payload()
    assert payload["type"] == "storyboard"
    assert payload["schema_version"] == STORYBOARD_ARTIFACT_SCHEMA_VERSION
    assert set(payload) == {
        "episode_id",
        "type",
        "schema_version",
        "source_script",
        "scenes",
        "total_duration_ms",
        "metadata",
    }
    assert "narration" not in payload["scenes"][0], "ナレーションは台本が単一の真実"


def test_producer_output_is_accepted_by_the_consumer() -> None:
    payload = _payload()
    parsed = parse_artifact(payload)
    assert isinstance(parsed, StoryboardArtifact)
    assert parse_storyboard_artifact(payload) == parsed
    assert parsed.model_dump(mode="json") == payload


def test_storyboard_is_registered_for_dispatch() -> None:
    assert ARTIFACT_MODELS[ArtifactType.STORYBOARD] is StoryboardArtifact


def test_extra_fields_are_forbidden() -> None:
    payload = _payload()
    with pytest.raises(ValidationError):
        parse_artifact({**payload, "unexpected": 1})
    scene = {**payload["scenes"][0], "narration": "複製しない"}
    with pytest.raises(ValidationError):
        parse_artifact({**payload, "scenes": [scene, *payload["scenes"][1:]]})


def test_models_are_frozen() -> None:
    parsed = parse_storyboard_artifact(_payload())
    with pytest.raises(ValidationError):
        parsed.total_duration_ms = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        parsed.scenes[0].duration_ms = 1  # type: ignore[misc]


def test_unknown_schema_version_is_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_artifact({**_payload(), "schema_version": "2.0"})


def test_visual_kind_vocabulary_is_closed() -> None:
    payload = _payload()
    scene = {**payload["scenes"][0], "visual_kind": "character_scene"}
    with pytest.raises(ValidationError):
        parse_artifact({**payload, "scenes": [scene, *payload["scenes"][1:]]})
    assert StoryboardVisualKind("character") is StoryboardVisualKind.CHARACTER


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda s: s[1].update(order=3), id="orders-not-consecutive"),
        pytest.param(lambda s: s[1].update(scene_id="sb9"), id="scene-id-not-from-order"),
        pytest.param(lambda s: s[0].update(start_ms=100), id="first-start-not-zero"),
        pytest.param(lambda s: s[2].update(start_ms=12_001), id="gap-between-scenes"),
    ],
)
def test_timeline_invariants(mutate) -> None:
    scenes = _scenes([6_000, 6_000, 6_000])
    mutate(scenes)
    with pytest.raises(ValidationError):
        _payload(scenes=scenes)


def test_sum_of_durations_must_equal_total() -> None:
    with pytest.raises(ValidationError):
        _payload(total_duration_ms=18_001)


@pytest.mark.parametrize("duration", [499, 20_001])
def test_scene_duration_bounds(duration: int) -> None:
    scenes = _scenes([duration, 15_000])
    with pytest.raises(ValidationError):
        _payload(scenes=scenes, total_duration_ms=duration + 15_000)


@pytest.mark.parametrize("total", [14_000, 61_000])
def test_total_duration_bounds(total: int) -> None:
    scenes = _scenes([total // 4] * 4)
    with pytest.raises(ValidationError):
        _payload(scenes=scenes, total_duration_ms=total)


def test_scene_count_upper_bound() -> None:
    count = STORYBOARD_MAX_SCENES + 1
    with pytest.raises(ValidationError):
        _payload(scenes=_scenes([1_000] * count), total_duration_ms=1_000 * count)


@pytest.mark.parametrize(
    "source",
    [
        {**_SOURCE, "sha256": "A" * 64},
        {**_SOURCE, "artifact_id": "not-a-uuid"},
        {**_SOURCE, "artifact_id": "0000000A-0000-0000-0000-00000000000B"},
        {**_SOURCE, "schema_version": "2.0"},
    ],
)
def test_source_script_is_pinned_strictly(source: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _payload(source_script=source)
