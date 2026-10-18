"""shorts_agentic_tools@1（ADR-0047 §2、INV-53 / INV-51）。

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import pytest

from contracts.agents import AGENT_SPECS, resolve_agent_spec
from contracts.intervention import AgentRole
from contracts.workflow_templates import TEMPLATES, template_hash
from domain.workflow_templates.compiler import (
    TemplateCompileError,
    compile_agentic_template,
    compile_template,
    plan_requires_agent_tools,
    validate_template,
)

#: 既存テンプレートの literal 固定（tool 版を足しても、pin 済みの計画・履歴・replay は変わらない）
SHORTS_AGENTIC_1_PLAN_HASH = "8a87be51e2055e73e871871ca78d1547fc944a105e4677f6142f257796f705b3"
SHORTS_AGENTIC_1_TEMPLATE_HASH = "75937b8afce67be11628821ffed9532deec7374f753fb7eb7ded031fd5587843"
DAILY_LEGACY_1_TEMPLATE_HASH = "f08b03f3ffbae02e1189fbdf853f6b8bfe084e644c0316a3dcfbff07d495d895"
SHORTS_REVIEWED_1_TEMPLATE_HASH = "4c8c903a240b053c202991a9d223cf0ebeaa6026f580b03d6163674c8b0e8150"

KEY = ("shorts_agentic_tools", "1")


def test_existing_templates_and_the_old_plan_hash_are_unchanged() -> None:
    assert template_hash(TEMPLATES[("shorts_agentic", "1")]) == SHORTS_AGENTIC_1_TEMPLATE_HASH
    assert template_hash(TEMPLATES[("daily_legacy", "1")]) == DAILY_LEGACY_1_TEMPLATE_HASH
    assert template_hash(TEMPLATES[("shorts_reviewed", "1")]) == SHORTS_REVIEWED_1_TEMPLATE_HASH
    plan = compile_template("shorts_agentic", "1")
    assert plan.plan_hash == SHORTS_AGENTIC_1_PLAN_HASH
    assert plan.template_hash == SHORTS_AGENTIC_1_TEMPLATE_HASH


def test_the_tool_template_has_exactly_the_adr_stage_sequence() -> None:
    t = TEMPLATES[KEY]
    assert t.runnable is True and t.execution == "agentic" and t.format_profile == "shorts_en"
    assert [(s.id, s.handler, s.agent_ref, s.after) for s in t.stages] == [
        ("strategy", "strategy_agent_v1", "strategy@1.1", ()),
        ("producer", "existing_producer_adopt", None, ("strategy",)),
        ("creative", "creative_director_v1", "creative_director@1.0", ("producer",)),
        ("script", "existing_script", None, ("creative",)),
        # 台本の事実確認は台本ができた後（入力は script）。storyboard は確認の後
        ("historian", "historian_agent_v1", "historian@1.0", ("script",)),
        ("storyboard", "existing_storyboard", None, ("historian",)),
        ("production", "existing_production", None, ("storyboard",)),
        ("render", "existing_render", None, ("production",)),
        ("private_upload", "existing_private_upload", None, ("render",)),
        ("quality", "quality_reviewer_v1", "quality_reviewer@1.0", ("private_upload",)),
    ]
    assert [(s.id, s.handler, s.agent_ref, s.trigger) for s in t.post_publish] == [
        ("lesson", "learning_agent_v1", "learning@1.1", "public_metrics_available")
    ]
    assert t.max_qa_repair_cycles == 0


def test_the_tool_template_is_valid_and_compiles_to_a_plan_pinning_the_tool_specs() -> None:
    validate_template(TEMPLATES[KEY], agent_specs=AGENT_SPECS)
    plan = compile_agentic_template(*KEY)
    by_stage = {s.stage_id: s for s in (*plan.stages, *plan.post_publish)}
    for stage_id, role, version in (
        ("strategy", AgentRole.STRATEGY, "1.1"),
        ("historian", AgentRole.HISTORIAN, "1.0"),
        ("lesson", AgentRole.LEARNING, "1.1"),
        ("creative", AgentRole.CREATIVE_DIRECTOR, "1.0"),
    ):
        spec = resolve_agent_spec(role, version)
        pinned = by_stage[stage_id]
        assert pinned.agent_spec_version == version
        assert pinned.agent_spec_hash == spec.manifest_hash
    order = [s.stage_id for s in plan.stages]
    assert order.index("script") < order.index("historian") < order.index("storyboard")
    assert plan.plan_hash != SHORTS_AGENTIC_1_PLAN_HASH
    assert compile_agentic_template(*KEY) == plan  # 決定的


def test_only_the_tool_template_requires_the_agent_tools_flag() -> None:
    assert plan_requires_agent_tools(compile_agentic_template(*KEY)) is True
    assert plan_requires_agent_tools(compile_agentic_template("shorts_agentic", "1")) is False


def test_a_template_may_not_skip_a_required_stage_even_with_a_historian() -> None:
    import dataclasses

    t = TEMPLATES[KEY]
    without_storyboard = dataclasses.replace(
        t, stages=tuple(s for s in t.stages if s.id != "storyboard")
    )
    with pytest.raises(TemplateCompileError) as err:
        validate_template(without_storyboard, agent_specs=AGENT_SPECS)
    assert err.value.code in {"missing_required_stage", "unknown_dependency"}
