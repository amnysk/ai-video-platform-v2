"""storyboard が台本を過不足なく覆うことの検査（ADR-0015）。"""

from __future__ import annotations

import pytest

from contracts.artifacts import (
    ScriptArtifact,
    StoryboardArtifact,
    build_script_artifact,
    build_storyboard_artifact,
)
from contracts.states import FailureClass
from contracts.topic_planning import SCRIPT_LOCALES, script_locale_for_language
from domain.errors import (
    StoryboardNarrationSpanTooShortError,
    StoryboardSchemaViolationError,
    classify_failure,
)
from domain.storyboard.coverage import (
    check_storyboard_covers_script,
    check_storyboard_fits_narration,
    script_scene_spans,
)


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


# ------------------------------------- 台本シーンの区間とナレーション（ADR-0026 追補）


def _en_script(narrations: tuple[str, ...], durations: tuple[int, ...]) -> ScriptArtifact:
    payload = build_script_artifact(
        episode_id="ep-1",
        language="en",
        title="t",
        hook="h",
        scenes=[
            {"id": f"s{i}", "narration": n, "visual": "v", "duration_ms": d}
            for i, (n, d) in enumerate(zip(narrations, durations, strict=True), 1)
        ],
        metadata={"topic": "t", "generator": "g", "generator_model": "m"},
    )
    return ScriptArtifact.model_validate(payload)


#: 19 語。en-US 1.9 語/秒で 10,000 ms 必要
_NINETEEN = " ".join(["word"] * 19)


def test_script_scene_spans_mirror_render_voice_placement() -> None:
    """区間 = 台本シーンの最初の storyboard シーンの開始 → 次の台本シーンの最初の開始。"""
    storyboard = _storyboard(["s1", "s1", "s2", "s3"], [2_000, 3_000, 7_000, 6_000])
    assert script_scene_spans(storyboard, _script()) == {"s1": 5_000, "s2": 7_000, "s3": 6_000}


def test_storyboard_span_shorter_than_the_narration_needs_is_a_retryable_defect() -> None:
    en = SCRIPT_LOCALES["en-US"]
    script = _en_script((_NINETEEN, "Short one.", "Short two."), (10_000, 6_000, 6_000))
    # 台本の総尺は守ったまま s1 の区間を 10,000 → 9,000 ms に縮める
    storyboard = _storyboard(["s1", "s2", "s3"], [9_000, 7_000, 6_000])
    with pytest.raises(StoryboardNarrationSpanTooShortError, match="s1") as caught:
        check_storyboard_fits_narration(storyboard, script, en)
    assert isinstance(caught.value, StoryboardSchemaViolationError)
    assert classify_failure(caught.value) is FailureClass.RETRYABLE


def test_storyboard_span_that_fits_the_narration_passes() -> None:
    en = SCRIPT_LOCALES["en-US"]
    script = _en_script((_NINETEEN, "Short one.", "Short two."), (10_000, 6_000, 6_000))
    assert en.required_speech_ms(_NINETEEN) == 10_000
    # s2 を縮めても s2 のナレーションは収まる。s1 はちょうど必要量
    storyboard = _storyboard(["s1", "s1", "s2", "s3"], [4_000, 6_000, 2_000, 10_000])
    check_storyboard_fits_narration(storyboard, script, en)


def test_required_speech_ms_agrees_with_the_narration_budget() -> None:
    """``span >= required_speech_ms`` と ``units <= narration_budget(span)`` は同じ判定。"""
    for locale in SCRIPT_LOCALES.values():
        for units in range(0, 200):
            word = locale.speech_unit.value == "word"
            narration = " ".join(["w"] * units) if word else "字" * units
            required = locale.required_speech_ms(narration)
            assert locale.narration_budget(required) >= units
            if required > 0:
                assert locale.narration_budget(required - 1) < units


def test_script_locale_is_looked_up_from_the_artifact_language() -> None:
    assert script_locale_for_language("en").locale == "en-US"
    assert script_locale_for_language("ja").locale == "ja-JP"
    with pytest.raises(ValueError):
        script_locale_for_language("xx")
