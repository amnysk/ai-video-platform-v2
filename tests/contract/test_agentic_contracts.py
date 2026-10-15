"""自律改善型制作の成果物契約（ADR-0043 §4）。

各テストの理由: docs/testing/jpmas2-p0a-rationale.md
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from contracts.agentic import (
    LESSON_TRANSITIONS,
    CreativeBrief,
    Lesson,
    LessonStatus,
    LessonUse,
    MeasurementWindow,
    QualityFinding,
    QualityReview,
    StrategyBrief,
    StrategyCandidate,
    UserBrief,
    WorkflowPlan,
)
from contracts.artifacts import ARTIFACT_MODELS
from contracts.states import ArtifactType
from contracts.topic_planning import DEFAULT_CONTENT_PROFILE_ID

FORBIDDEN_WORDS = ("budget", "visibility", "privacy", "schedule", "publish", "cost", "price")


def candidate(id_: str = "c1", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "title": "Why the Shogun hid in plain sight",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
        "rationale": "Strong curiosity gap.",
        "status": "proposed",
    }
    return {**base, **over}


def strategy(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "episode_id": "ep-1",
        "audience_hypothesis": "US 20s like power-structure stories.",
        "audience_basis": "assumed",
        "candidates": [candidate("c1"), candidate("c2")],
        "recommended_candidate_id": "c1",
    }
    return {**base, **over}


def test_user_brief_has_no_budget_visibility_or_schedule_field() -> None:
    for name in UserBrief.model_fields:
        assert not any(w in name for w in FORBIDDEN_WORDS), name
    with pytest.raises(ValidationError):
        UserBrief(theme="x", template_id="t", template_version="1", visibility="public")  # type: ignore[call-arg]


def test_user_brief_defaults_and_limits() -> None:
    brief = UserBrief(theme="Ninja myths", template_id="shorts_agentic", template_version="1")
    assert brief.target_language == "en-US"
    assert brief.target_audience == "US viewers in their 20s"
    assert brief.format_profile == "shorts_en"
    assert brief.strategy_profile_id == "us_young_history_v1"
    assert brief.content_profile_id == DEFAULT_CONTENT_PROFILE_ID
    base = {"theme": "x", "template_id": "t", "template_version": "1"}
    for bad in (
        {"theme": ""},
        {"theme": "x" * 201},
        {"desired_duration_seconds": 14},
        {"desired_duration_seconds": 601},
        {"tone": "x" * 201},
        {"must_keep": ["a"] * 11},
        {"must_keep": ["x" * 201]},
        {"notes": "x" * 2001},
        {"target_language": "ja-JP"},
    ):
        with pytest.raises(ValidationError):
            UserBrief.model_validate({**base, **bad})


def test_strategy_brief_round_trips() -> None:
    brief = StrategyBrief.model_validate(strategy())
    assert StrategyBrief.model_validate_json(brief.model_dump_json()) == brief


def test_strategy_candidates_are_one_to_five_with_unique_ids() -> None:
    with pytest.raises(ValidationError):
        StrategyBrief.model_validate(strategy(candidates=[]))
    six = [candidate(f"c{i}") for i in range(6)]
    with pytest.raises(ValidationError):
        StrategyBrief.model_validate(strategy(candidates=six))
    with pytest.raises(ValidationError, match="unique"):
        StrategyBrief.model_validate(strategy(candidates=[candidate("c1"), candidate("c1")]))


def test_the_recommendation_must_be_a_proposed_candidate() -> None:
    with pytest.raises(ValidationError, match="not a candidate"):
        StrategyBrief.model_validate(strategy(recommended_candidate_id="zz"))
    rejected = candidate("c2", status="rejected", rejected_reason="too generic")
    with pytest.raises(ValidationError, match="proposed"):
        StrategyBrief.model_validate(
            strategy(candidates=[candidate("c1"), rejected], recommended_candidate_id="c2")
        )


def test_rejected_reason_is_required_iff_rejected() -> None:
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate(candidate(status="rejected"))
    with pytest.raises(ValidationError):
        StrategyCandidate.model_validate(candidate(rejected_reason="why"))
    StrategyCandidate.model_validate(candidate(status="rejected", rejected_reason="why"))


@pytest.mark.parametrize(
    ("status", "used_as", "ok"),
    [
        ("validated", "validated_reference", True),
        ("validated", "hypothesis", True),
        ("proposed", "hypothesis", True),
        ("testing", "validated_reference", False),
        ("proposed", "validated_reference", False),
        ("rejected", "validated_reference", False),
    ],
)
def test_only_a_validated_lesson_is_a_validated_reference(
    status: str, used_as: str, ok: bool
) -> None:
    data = {"lesson_revision_id": "r1", "lesson_status": status, "used_as": used_as}
    if ok:
        LessonUse.model_validate(data)
    else:
        with pytest.raises(ValidationError):
            LessonUse.model_validate(data)


def creative(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "episode_id": "ep-1",
        "adopted_candidate_id": "c1",
        "topic": "Why the Shogun hid in plain sight",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
        "strategy_profile_id": "us_young_history_v1",
        "content_profile_id": "shorts",
        "hook_options": ["He ruled Japan without ever being king."],
        "tone": "wry",
    }
    return {**base, **over}


def test_creative_brief_fields_are_compatible_with_topic_plan_limits() -> None:
    CreativeBrief.model_validate(creative())
    for bad in (
        {"subject": "Not A Slug"},
        {"era": "x" * 41},
        {"hook": "x" * 301},
        {"hook_options": []},
        {"hook_options": ["hello world"] * 4},
        {"topic": "x" * 201},
        {"angle": "not-an-angle"},
    ):
        with pytest.raises(ValidationError):
            CreativeBrief.model_validate(creative(**bad))


def finding(id_: str = "f1", **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "severity": "minor",
        "category": "visual",
        "evidence": "Scene 2 is too dark.",
        "confidence": 0.6,
    }
    return {**base, **over}


def test_quality_review_is_always_self_assessed() -> None:
    review = QualityReview.model_validate(
        {"episode_id": "ep-1", "overall": "pass", "findings": [finding(severity="info")]}
    )
    assert review.assessment_kind == "self_assessed"
    with pytest.raises(ValidationError):
        QualityReview.model_validate(
            {"episode_id": "ep-1", "overall": "pass", "assessment_kind": "audience_measured"}
        )


def test_quality_finding_limits() -> None:
    cases: list[dict[str, Any]] = [
        {"confidence": 1.1},
        {"confidence": -0.1},
        {"severity": "fatal"},
        {"category": "music"},
        {"recommended_visual_description": "x" * 601},
        {"time_range": {"start_ms": 500, "end_ms": 500}},
    ]
    for bad in cases:
        with pytest.raises(ValidationError):
            QualityFinding.model_validate(finding(**bad))
    QualityFinding.model_validate(finding(time_range={"start_ms": 0, "end_ms": 900}))


def test_quality_review_consistency() -> None:
    with pytest.raises(ValidationError, match="inputs_missing"):
        QualityReview.model_validate({"episode_id": "e", "overall": "blocked_missing_input"})
    QualityReview.model_validate(
        {"episode_id": "e", "overall": "blocked_missing_input", "inputs_missing": ["final_video"]}
    )
    with pytest.raises(ValidationError, match="cannot pass"):
        QualityReview.model_validate(
            {"episode_id": "e", "overall": "pass", "findings": [finding(severity="major")]}
        )
    with pytest.raises(ValidationError, match="unique"):
        QualityReview.model_validate(
            {"episode_id": "e", "overall": "needs_changes", "findings": [finding(), finding()]}
        )


def test_lesson_transitions_are_exactly_the_documented_table() -> None:
    S = LessonStatus
    assert {
        S.PROPOSED: {S.TESTING, S.REJECTED, S.RETIRED},
        S.TESTING: {S.VALIDATED, S.REJECTED, S.RETIRED},
        S.VALIDATED: {S.RETIRED},
        S.REJECTED: set(),
        S.RETIRED: set(),
    } == LESSON_TRANSITIONS
    assert set(LESSON_TRANSITIONS) == set(S)


def test_a_measurement_window_without_metrics_needs_a_reason() -> None:
    with pytest.raises(ValidationError, match="missing_reason"):
        MeasurementWindow(window="48h")
    MeasurementWindow(window="48h", missing_reason="video is private")
    # 0 は欠測ではない（測定できた 0）
    MeasurementWindow(window="7d", views=0)
    with pytest.raises(ValidationError):
        MeasurementWindow(window="14d", views=1)  # type: ignore[arg-type]


def test_lesson_windows_are_unique_and_times_are_timezone_aware() -> None:
    w = {"window": "48h", "missing_reason": "private"}
    base: dict[str, Any] = {
        "episode_id": "e",
        "video_id": "abc",
        "scope": "shorts only",
        "status": "proposed",
    }
    Lesson.model_validate({**base, "measurement_windows": [w], "published_at": datetime.now(UTC)})
    with pytest.raises(ValidationError, match="unique"):
        Lesson.model_validate({**base, "measurement_windows": [w, w]})
    with pytest.raises(ValidationError):
        Lesson.model_validate({**base, "published_at": datetime(2026, 1, 1)})


def test_every_new_model_is_frozen_strict_and_versioned() -> None:
    models = (UserBrief, StrategyBrief, CreativeBrief, QualityReview, Lesson, WorkflowPlan)
    for model in models:
        assert model.model_config.get("extra") == "forbid", model
        assert model.model_config.get("frozen") is True, model
        assert model.model_fields["schema_version"].annotation is not str, model


def test_new_types_are_registered_episode_scoped_models() -> None:
    from infrastructure.db import models as db_models

    expected = {
        ArtifactType.USER_BRIEF: UserBrief,
        ArtifactType.STRATEGY_BRIEF: StrategyBrief,
        ArtifactType.CREATIVE_BRIEF: CreativeBrief,
        ArtifactType.QUALITY_REVIEW: QualityReview,
        ArtifactType.LESSON: Lesson,
        ArtifactType.WORKFLOW_PLAN: WorkflowPlan,
    }
    for artifact_type, model in expected.items():
        assert ARTIFACT_MODELS[artifact_type] is model
        assert artifact_type not in db_models.SCENE_ARTIFACT_TYPES
