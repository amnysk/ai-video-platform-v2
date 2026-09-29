"""拒否されたシーンの代替案の採否規則と上限（ADR-0035, INV-34）。純粋関数なので unit。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from contracts.artifacts import StoryboardScene, VisualSubject
from contracts.states import RejectedInput, RejectionCategory
from domain.errors import SceneAlternativeInvalidError, SceneAlternativeLimitReachedError
from domain.production.scene_alternative import (
    CostEntry,
    PreviousAlternative,
    RejectionFact,
    SceneAlternativeContext,
    SceneAlternativeInfeasible,
    SceneAlternativeProposal,
    allowed_subjects,
    check_recovery_limits,
    parse_planner_output,
    recovery_cost_usd,
    scene_alternative_input_hash,
    validate_proposal,
)

LIKENESS = RejectionFact(
    id="r1",
    rejected_input=RejectedInput.IMAGE,
    types=("content_policy_violation",),
    reason="partner_validation_failed",
    message="may contain likenesses of real people",
    category=RejectionCategory.CONTENT_POLICY,
)
SCENE = StoryboardScene.model_validate(
    {
        "scene_id": "sb2",
        "order": 2,
        "script_scene_id": "s1",
        "start_ms": 4000,
        "duration_ms": 4000,
        "visual_kind": "generated",
        "visual_description": "Ieyasu stands on a low ridge in armor",
    }
)


def _context(**overrides) -> SceneAlternativeContext:
    fields = {
        "episode_id": "ep",
        "scene": SCENE,
        "original_description": SCENE.visual_description,
        "narration": "In 1600 Tokugawa Ieyasu won at Sekigahara.",
        "language": "en-US",
        "rejections": (LIKENESS,),
        "previous": (),
        "allowed_subjects": allowed_subjects((LIKENESS,)),
    }
    fields.update(overrides)
    return SceneAlternativeContext(**fields)


def _proposal(**overrides) -> SceneAlternativeProposal:
    fields = {
        "visual_kind": "broll",
        "visual_subject": VisualSubject.LANDSCAPE,
        "visual_description": "The Sekigahara valley at dawn, banners in the mist, no figures",
        "framing": "wide",
        "camera_movement": None,
        "rationale": "The battlefield shows the event; the narration names Ieyasu.",
    }
    fields.update(overrides)
    return SceneAlternativeProposal(**fields)


# ------------------------------------------------------------------ 映像対象の許可


def test_content_policy_rejection_rules_out_person_subjects() -> None:
    allowed = allowed_subjects((LIKENESS,))
    assert VisualSubject.NAMED_PERSON not in allowed
    assert VisualSubject.FIGURE_ANONYMOUS not in allowed
    assert {VisualSubject.SITE, VisualSubject.MAP, VisualSubject.DOCUMENT} <= set(allowed)


def test_other_rejections_do_not_rule_out_people() -> None:
    """人物の一律禁止はしない。根拠（内容方針の拒否）がある場合だけ外す。"""
    other = RejectionFact("r2", RejectedInput.PROMPT, ("invalid_input",), None, None)
    assert VisualSubject.NAMED_PERSON in allowed_subjects((other,))


# ------------------------------------------------------------------ planner 出力の解釈


def test_parse_feasible_plan() -> None:
    text = json.dumps(
        {
            "feasible": True,
            "visual_kind": "broll",
            "visual_subject": "landscape",
            "visual_description": "A valley",
            "framing": "wide",
            "camera_movement": None,
            "rationale": "why",
        }
    )
    plan = parse_planner_output(f"```json\n{text}\n```")
    assert isinstance(plan, SceneAlternativeProposal)
    assert plan.visual_subject is VisualSubject.LANDSCAPE and plan.camera_movement is None


def test_parse_infeasible_plan_keeps_the_reason() -> None:
    plan = parse_planner_output('{"feasible": false, "reason": "the scene is about his face"}')
    assert isinstance(plan, SceneAlternativeInfeasible)
    assert plan.reason == "the scene is about his face"


@pytest.mark.parametrize(
    "text",
    [
        "no json here",
        '{"feasible": "yes"}',
        '{"feasible": false}',
        '{"feasible": true, "visual_kind": "broll"}',
        '{"feasible": true, "visual_kind": "broll", "visual_subject": "portrait",'
        ' "visual_description": "x"}',
    ],
)
def test_malformed_planner_output_is_not_repaired(text: str) -> None:
    with pytest.raises(SceneAlternativeInvalidError):
        parse_planner_output(text)


# ------------------------------------------------------------------ 採否


def test_a_good_alternative_passes() -> None:
    validate_proposal(_proposal(), _context())


def test_a_person_subject_after_a_likeness_rejection_is_refused() -> None:
    with pytest.raises(SceneAlternativeInvalidError, match="not allowed"):
        validate_proposal(_proposal(visual_subject=VisualSubject.NAMED_PERSON), _context())


def test_repeating_the_rejected_description_is_refused() -> None:
    """同じ画像を言い換えずに作り直すだけの案は、同じ判定を繰り返すだけなので採らない。"""
    same = _proposal(visual_description="  ieyasu stands on a LOW ridge in armor ")
    with pytest.raises(SceneAlternativeInvalidError, match="already tried"):
        validate_proposal(same, _context())


def test_repeating_a_previous_alternative_is_refused() -> None:
    previous = PreviousAlternative(1, VisualSubject.MAP, "A map of the armies")
    with pytest.raises(SceneAlternativeInvalidError, match="already tried"):
        validate_proposal(
            _proposal(visual_description="a map of the armies"), _context(previous=(previous,))
        )


def test_an_alternative_without_rationale_is_refused() -> None:
    with pytest.raises(SceneAlternativeInvalidError, match="explain"):
        validate_proposal(_proposal(rationale=""), _context())


# ------------------------------------------------------------------ 費用と上限（INV-34）

T0 = datetime(2026, 9, 28, 21, 0, tzinfo=UTC)


def test_recovery_cost_counts_rejected_spends_and_rebuilds_only() -> None:
    entries = [
        CostEntry("sb1", Decimal("0.97"), False, T0),  # 通常の成功: 数えない
        CostEntry("sb2", Decimal("0.97"), True, T0),  # 拒否された試行: 数える
        CostEntry("sb2", Decimal("0.04"), False, T0 + timedelta(minutes=5)),  # 作り直し
        CostEntry("sb2", Decimal("0.97"), False, T0 + timedelta(minutes=6)),  # 作り直し
        CostEntry("sb3", Decimal("0.97"), False, T0 + timedelta(minutes=7)),  # 無関係
    ]
    first = {"sb2": T0 + timedelta(minutes=4)}
    assert recovery_cost_usd(entries, first) == Decimal("1.98")


def test_limits_allow_the_first_alternative() -> None:
    check_recovery_limits(
        scene_alternatives=0,
        episode_alternatives=0,
        recovery_cost=Decimal("0.97"),
        projected_cost=Decimal("1.01"),
    )


@pytest.mark.parametrize(
    ("scene", "episode", "cost", "projected", "match"),
    [
        (2, 2, "0", "0", "this scene"),
        (0, 3, "0", "0", "this episode"),
        (0, 0, "4.50", "1.01", "cap"),
    ],
)
def test_limits_stop_automation(scene, episode, cost, projected, match) -> None:
    with pytest.raises(SceneAlternativeLimitReachedError, match=match):
        check_recovery_limits(
            scene_alternatives=scene,
            episode_alternatives=episode,
            recovery_cost=Decimal(cost),
            projected_cost=Decimal(projected),
            max_per_scene=2,
            max_per_episode=3,
            max_cost_usd=5.0,
        )


def test_planner_input_hash_changes_with_new_rejections() -> None:
    base = {
        "episode_id": "ep",
        "scene_id": "sb2",
        "scene_fingerprint": "f",
        "rejection_ids": ["r1"],
        "previous_override_sha256s": [],
        "planner_profile_id": "p",
    }
    assert scene_alternative_input_hash(**base) != scene_alternative_input_hash(
        **{**base, "rejection_ids": ["r1", "r2"]}
    )
    assert scene_alternative_input_hash(**base) == scene_alternative_input_hash(
        **{**base, "rejection_ids": ["r1"]}
    )
