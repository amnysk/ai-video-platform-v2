"""storyboard が台本を過不足なく覆うことの検査（ADR-0015）。"""

from __future__ import annotations

import pytest

from contracts.artifacts import (
    ScriptArtifact,
    StoryboardArtifact,
    build_script_artifact,
    build_storyboard_artifact,
)
from domain.errors import StoryboardSchemaViolationError
from domain.storyboard.coverage import check_storyboard_covers_script


def _script() -> ScriptArtifact:
    payload = build_script_artifact(
        episode_id="ep-1",
        language="ja",
        title="t",
        hook="h",
        scenes=[
            {"id": f"s{i}", "narration": "n", "visual": "v", "duration_ms": 6_000}
            for i in (1, 2, 3)
        ],
        metadata={"topic": "t", "generator": "g", "generator_model": "m"},
    )
    return ScriptArtifact.model_validate(payload)


def _storyboard(script_ids: list[str], durations: list[int]) -> StoryboardArtifact:
    scenes = []
    start = 0
    for order, (sid, duration) in enumerate(zip(script_ids, durations, strict=True), start=1):
        scenes.append(
            {
                "scene_id": f"sb{order}",
                "order": order,
                "script_scene_id": sid,
                "start_ms": start,
                "duration_ms": duration,
                "visual_kind": "animation",
                "visual_description": "v",
            }
        )
        start += duration
    payload = build_storyboard_artifact(
        episode_id="ep-1",
        source_script={
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "sha256": "c" * 64,
            "schema_version": "1.0",
        },
        scenes=scenes,
        total_duration_ms=start,
        metadata={"generator": "g", "generator_model": "m", "generation_spec_id": "s"},
    )
    return StoryboardArtifact.model_validate(payload)


def test_one_to_one_coverage_passes() -> None:
    check_storyboard_covers_script(_storyboard(["s1", "s2", "s3"], [6_000] * 3), _script())


def test_a_script_scene_may_span_several_storyboard_scenes() -> None:
    storyboard = _storyboard(["s1", "s1", "s2", "s3"], [3_000, 3_000, 6_000, 6_000])
    check_storyboard_covers_script(storyboard, _script())


@pytest.mark.parametrize(
    ("ids", "durations"),
    [
        pytest.param(["s1", "s3"], [9_000, 9_000], id="missing-script-scene"),
        pytest.param(["s1", "s3", "s2"], [6_000] * 3, id="goes-backwards"),
        pytest.param(["s1", "s2", "s9"], [6_000] * 3, id="unknown-script-scene"),
        pytest.param(["s1", "s2", "s3"], [6_000, 6_000, 7_000], id="total-mismatch"),
    ],
)
def test_violations_raise(ids: list[str], durations: list[int]) -> None:
    with pytest.raises(StoryboardSchemaViolationError):
        check_storyboard_covers_script(_storyboard(ids, durations), _script())
