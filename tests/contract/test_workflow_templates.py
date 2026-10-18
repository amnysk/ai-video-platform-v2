"""Workflow Template の契約（ADR-0043 §3、INV-53）。理由: docs/testing/jpmas2-p0a-rationale.md"""

from __future__ import annotations

import dataclasses

from contracts.agentic import PlannedStage, WorkflowPlan
from contracts.agents import AgentHandlerRef
from contracts.workflow_templates import (
    HARD_MAX_QA_REPAIR_CYCLES,
    REQUIRED_STAGE_ORDER,
    TEMPLATES,
    ExistingStageHandler,
    TemplateStage,
    WorkflowTemplate,
    template_hash,
)

FORBIDDEN_WORDS = ("budget", "visibility", "privacy", "schedule", "publish_at", "cost", "price")


def _names(cls: type) -> list[str]:
    if dataclasses.is_dataclass(cls):
        return [f.name for f in dataclasses.fields(cls)]
    return list(cls.model_fields)  # type: ignore[attr-defined]


def test_no_template_or_plan_field_can_express_visibility_budget_or_schedule() -> None:
    for cls in (TemplateStage, WorkflowTemplate, PlannedStage, WorkflowPlan):
        for name in _names(cls):
            assert not any(w in name for w in FORBIDDEN_WORDS), (cls.__name__, name)


def test_the_registered_templates_are_exactly_the_adr_templates() -> None:
    # ADR-0043 の 3 つ + ADR-0047 の shorts_agentic_tools@1（tool 版。内容は別テストが固定する）
    assert set(TEMPLATES) == {
        ("daily_legacy", "1"),
        ("shorts_agentic", "1"),
        ("shorts_reviewed", "1"),
        ("shorts_agentic_tools", "1"),
    }
    assert TEMPLATES[("daily_legacy", "1")].execution == "legacy_pipeline"
    assert TEMPLATES[("shorts_agentic", "1")].runnable is True
    assert TEMPLATES[("shorts_reviewed", "1")].runnable is False
    assert TEMPLATES[("shorts_agentic", "1")].max_qa_repair_cycles == 0
    assert (
        TEMPLATES[("shorts_reviewed", "1")].max_qa_repair_cycles == 2 == HARD_MAX_QA_REPAIR_CYCLES
    )


def test_shorts_agentic_has_exactly_the_adr_stage_sequence() -> None:
    t = TEMPLATES[("shorts_agentic", "1")]
    assert [(s.id, s.handler, s.agent_ref) for s in t.stages] == [
        ("strategy", "strategy_agent_v1", "strategy@1.0"),
        ("producer", "existing_producer_adopt", None),
        ("creative", "creative_director_v1", "creative_director@1.0"),
        ("script", "existing_script", None),
        ("storyboard", "existing_storyboard", None),
        ("production", "existing_production", None),
        ("render", "existing_render", None),
        ("private_upload", "existing_private_upload", None),
        ("quality", "quality_reviewer_v1", "quality_reviewer@1.0"),
    ]
    assert [(s.id, s.handler, s.agent_ref, s.trigger) for s in t.post_publish] == [
        ("lesson", "learning_agent_v1", "learning@1.0", "public_metrics_available")
    ]


def test_shorts_reviewed_reviews_before_the_private_upload() -> None:
    t = TEMPLATES[("shorts_reviewed", "1")]
    by_id = {s.id: s for s in t.stages}
    assert "quality" in by_id["private_upload"].after


def test_template_handlers_come_from_the_two_registered_vocabularies() -> None:
    known = {h.value for h in AgentHandlerRef} | {h.value for h in ExistingStageHandler}
    for t in TEMPLATES.values():
        for s in (*t.stages, *t.post_publish):
            assert s.handler in known
    assert [h.value for h in REQUIRED_STAGE_ORDER] == [
        "existing_script",
        "existing_storyboard",
        "existing_production",
        "existing_render",
        "existing_private_upload",
    ]


def test_template_hash_is_stable_and_sensitive() -> None:
    t = TEMPLATES[("shorts_agentic", "1")]
    assert template_hash(t) == template_hash(dataclasses.replace(t))
    assert template_hash(t) != template_hash(dataclasses.replace(t, max_qa_repair_cycles=1))
    assert template_hash(t) != template_hash(TEMPLATES[("shorts_reviewed", "1")])
