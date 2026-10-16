"""agentic 実行のための Template コンパイル入口と、script brief の変換（ADR-0043 §6）。"""

from __future__ import annotations

import pytest

from contracts.agentic_pipeline import EXISTING_HANDLER_STAGE
from contracts.application import OperationErrorCode
from contracts.pipeline import PipelineStage
from contracts.topic_planning import DEFAULT_CONTENT_PROFILE_ID, DEFAULT_STRATEGY_PROFILE_ID
from contracts.workflow_templates import REQUIRED_STAGE_ORDER, ExistingStageHandler
from domain.errors import PromptContractError
from domain.script.brief import PLAN_FIELDS, creative_brief, plan_brief
from domain.workflow_templates.compiler import (
    TemplateCompileError,
    compile_agentic_template,
    operation_code_for,
)


def test_shorts_agentic_compiles_to_an_agentic_plan() -> None:
    plan = compile_agentic_template("shorts_agentic", "1")
    assert plan.execution == "agentic"
    assert [s.stage_id for s in plan.stages][:4] == ["strategy", "producer", "creative", "script"]


@pytest.mark.parametrize(
    ("template", "version", "code"),
    [
        ("nope", "1", OperationErrorCode.UNKNOWN_TEMPLATE),
        ("shorts_agentic", "9", OperationErrorCode.UNKNOWN_TEMPLATE),
        ("shorts_reviewed", "1", OperationErrorCode.TEMPLATE_NOT_RUNNABLE),
        ("daily_legacy", "1", OperationErrorCode.TEMPLATE_NOT_RUNNABLE),
    ],
)
def test_unknown_unrunnable_and_legacy_templates_are_refused(
    template: str, version: str, code: OperationErrorCode
) -> None:
    with pytest.raises(TemplateCompileError) as err:
        compile_agentic_template(template, version)
    assert operation_code_for(err.value) is code


def test_every_other_compile_error_is_an_invalid_payload() -> None:
    assert (
        operation_code_for(TemplateCompileError("cycle", "x")) is OperationErrorCode.INVALID_PAYLOAD
    )


def test_every_required_existing_stage_maps_to_a_pipeline_stage() -> None:
    """既存工程の handler -> PipelineStage の写像が、必須工程の順序と食い違わない。"""
    required = [EXISTING_HANDLER_STAGE[h.value] for h in REQUIRED_STAGE_ORDER]
    assert required == list(PipelineStage)
    assert ExistingStageHandler.PRODUCER_ADOPT.value not in EXISTING_HANDLER_STAGE


# ------------------------------------------------------------------ CreativeBrief -> ScriptBrief
_FIELDS = {
    "strategy_profile_id": "us_en_default",
    "content_profile_id": "history_short",
    "topic": "The Roman Empire Had a Secret Postal Network",
    "subject": "cursus-publicus",
    "angle": "hidden_cause",
    "era": "roman-empire",
    "hook": "Letters crossed an empire faster than you think.",
}


def _profiles() -> tuple[str, str]:
    return DEFAULT_STRATEGY_PROFILE_ID, DEFAULT_CONTENT_PROFILE_ID


def test_a_creative_brief_script_brief_matches_the_plan_brief_without_a_plan_id() -> None:
    strategy, content = _profiles()
    fields = {**_FIELDS, "strategy_profile_id": strategy, "content_profile_id": content}
    from_creative = creative_brief(**fields)
    from_plan = plan_brief(topic_plan_id="p", **fields)
    assert from_creative.topic_plan_id is None
    assert from_creative.locale == from_plan.locale
    assert from_creative.content_profile == from_plan.content_profile
    assert from_creative.subject_matter == from_plan.subject_matter
    assert tuple(from_creative.subject_matter) == PLAN_FIELDS


def test_a_creative_brief_with_an_unknown_profile_is_refused_not_defaulted() -> None:
    strategy, content = _profiles()
    with pytest.raises(PromptContractError):
        creative_brief(**{**_FIELDS, "strategy_profile_id": "nope", "content_profile_id": content})
    with pytest.raises(PromptContractError):
        creative_brief(**{**_FIELDS, "strategy_profile_id": strategy, "content_profile_id": "nope"})
