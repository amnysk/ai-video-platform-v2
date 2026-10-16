"""操作契約（ADR-0043 §1）。理由は docs/testing/jpmas2-p0b-rationale.md。"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from contracts.application import (
    STORYBOARD_PROPOSAL_NAMESPACE,
    OperationErrorCode,
    StoryboardPatchProposal,
    StoryboardSceneEdit,
    storyboard_edit_command_id,
)
from contracts.intervention import RejectReason, SceneOverrideVisual


def test_shared_error_codes_are_spelled_like_reject_reasons() -> None:
    reject = {r.name: r.value for r in RejectReason}
    for code in OperationErrorCode:
        if code.name in reject:
            assert code.value == reject[code.name]
    assert {"invalid_payload", "unsupported_change", "forbidden", "budget_exceeded"} <= {
        c.value for c in OperationErrorCode
    }


def test_adr_error_vocabulary_is_exact() -> None:
    assert {c.value for c in OperationErrorCode} == {
        "invalid_payload", "unsupported_change", "missing_scope", "episode_not_found",
        "command_id_reused", "stale_control_revision", "stale_target_revision", "forbidden",
        "budget_exceeded", "budget_unverifiable", "feature_disabled", "unknown_template",
        "unknown_agent_spec", "template_not_runnable",
        # ADR-0044 §1（P2-A）
        "invalid_state", "change_set_not_found", "cost_not_estimated",
    }  # fmt: skip


def _proposal(**edit: object) -> dict:
    return {
        "episode_id": str(uuid.uuid4()),
        "base_storyboard_revision_id": "rev-1",
        "edits": [{"scene_id": "sb1", "visual_description": "x", **edit}],
    }


def test_visual_description_limit_is_the_override_limit() -> None:
    limit = 600
    assert SceneOverrideVisual.model_fields["visual_description"].metadata
    StoryboardPatchProposal.model_validate(_proposal(visual_description="a" * limit))
    with pytest.raises(ValidationError):
        StoryboardPatchProposal.model_validate(_proposal(visual_description="a" * (limit + 1)))
    schema = StoryboardSceneEdit.model_json_schema()["properties"]["visual_description"]
    assert {b.get("maxLength") for b in schema["anyOf"]} >= {limit}


def test_edits_are_bounded_and_extra_fields_forbidden() -> None:
    base = _proposal()
    StoryboardPatchProposal.model_validate({**base, "edits": [base["edits"][0]] * 12})
    for bad in ([], [base["edits"][0]] * 13):
        with pytest.raises(ValidationError):
            StoryboardPatchProposal.model_validate({**base, "edits": bad})
    with pytest.raises(ValidationError):
        StoryboardPatchProposal.model_validate({**base, "surprise": 1})


def test_edit_command_ids_are_deterministic_per_request_and_index() -> None:
    a = storyboard_edit_command_id("req-00000001", 0)
    assert a == str(uuid.uuid5(STORYBOARD_PROPOSAL_NAMESPACE, "req-00000001:0"))
    assert a == storyboard_edit_command_id("req-00000001", 0)
    assert a != storyboard_edit_command_id("req-00000001", 1)
    assert a != storyboard_edit_command_id("req-00000002", 0)
