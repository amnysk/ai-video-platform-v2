"""storyboard 時間軸の正規化（純粋関数）。"""

from __future__ import annotations

import pytest

from contracts.artifacts import StoryboardVisualKind, build_storyboard_artifact
from domain.errors import StoryboardSchemaViolationError
from domain.storyboard.normalize import assign_scene_identity, normalize_timeline
from domain.storyboard.ports import StoryboardSceneDraft


def _draft(script_scene_id: str, start_ms: int, duration_ms: int) -> StoryboardSceneDraft:
    return StoryboardSceneDraft(
        script_scene_id=script_scene_id,
        start_ms=start_ms,
        duration_ms=duration_ms,
        visual_kind=StoryboardVisualKind.DIAGRAM,
        visual_description="図解",
    )


def _spans(drafts) -> list[tuple[int, int]]:
    return [(d.start_ms, d.start_ms + d.duration_ms) for d in drafts]


def test_contiguous_timeline_is_unchanged() -> None:
    drafts = [_draft("s1", 0, 6_000), _draft("s2", 6_000, 6_000), _draft("s3", 12_000, 6_000)]
    assert normalize_timeline(drafts, 18_000) == tuple(drafts)


def test_small_gaps_overlaps_and_first_start_are_snapped() -> None:
    drafts = [_draft("s1", 400, 5_600), _draft("s2", 6_300, 5_700), _draft("s3", 11_600, 6_400)]
    result = normalize_timeline(drafts, 18_000)
    assert _spans(result) == [(0, 6_000), (6_000, 12_000), (12_000, 18_000)]


def test_last_end_within_tolerance_snaps_to_script_total() -> None:
    drafts = [_draft("s1", 0, 9_000), _draft("s2", 9_000, 9_900)]
    assert _spans(normalize_timeline(drafts, 18_000))[-1] == (9_000, 18_000)
    drafts = [_draft("s1", 0, 9_000), _draft("s2", 9_000, 10_000)]
    assert _spans(normalize_timeline(drafts, 18_000))[-1] == (9_000, 18_000)


@pytest.mark.parametrize(
    "drafts",
    [
        pytest.param([], id="empty"),
        pytest.param([_draft("s1", 501, 17_499)], id="first-start-too-late"),
        pytest.param([_draft("s1", 0, 6_000), _draft("s2", 6_501, 11_499)], id="gap-too-big"),
        pytest.param([_draft("s1", 0, 9_000), _draft("s2", 8_499, 9_501)], id="overlap-too-big"),
        pytest.param([_draft("s1", 0, 9_000), _draft("s2", 9_000, 10_001)], id="end-too-late"),
        pytest.param([_draft("s1", 0, 9_000), _draft("s2", 9_000, 7_999)], id="end-too-early"),
        pytest.param(
            [_draft("s1", 6_000, 6_000), _draft("s2", 0, 6_000)], id="unordered-not-sorted"
        ),
        pytest.param([_draft("s1", 0, 17_700), _draft("s2", 17_700, 300)], id="duration-too-short"),
        pytest.param([_draft("s1", 0, -1)], id="negative-duration"),
    ],
)
def test_violations_raise_retryable_schema_error(drafts) -> None:
    with pytest.raises(StoryboardSchemaViolationError):
        normalize_timeline(drafts, 18_000)


def test_assigned_identity_builds_a_valid_artifact() -> None:
    drafts = normalize_timeline([_draft("s1", 200, 8_800), _draft("s2", 9_100, 9_000)], 18_000)
    scenes = assign_scene_identity(drafts)
    assert [(s["scene_id"], s["order"]) for s in scenes] == [("sb1", 1), ("sb2", 2)]
    build_storyboard_artifact(
        episode_id="ep-1",
        source_script={
            "artifact_id": "00000000-0000-0000-0000-000000000001",
            "sha256": "b" * 64,
            "schema_version": "1.0",
        },
        scenes=scenes,
        total_duration_ms=18_000,
        metadata={"generator": "g", "generator_model": "m", "generation_spec_id": "s"},
    )
