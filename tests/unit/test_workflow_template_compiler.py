"""Workflow Template コンパイラ（ADR-0043 §3、INV-53）。

理由: docs/testing/jpmas2-p0a-rationale.md
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from contracts.agentic import WorkflowPlan
from contracts.agents import AGENT_SPECS
from contracts.intervention import AgentRole
from contracts.workflow_templates import TEMPLATES, TemplateStage, WorkflowTemplate
from domain.workflow_templates.compiler import (
    TemplateCompileError,
    compile_template,
    validate_template,
)

AGENTIC = TEMPLATES[("shorts_agentic", "1")]


def _tpl(**over: object) -> WorkflowTemplate:
    return dataclasses.replace(AGENTIC, **over)  # type: ignore[arg-type]


def _stages(**replace: TemplateStage | None) -> tuple[TemplateStage, ...]:
    """AGENTIC の stage を id で差し替え（None なら削除）。"""
    out: list[TemplateStage] = []
    for s in AGENTIC.stages:
        if s.id in replace:
            new = replace[s.id]
            if new is not None:
                out.append(new)
        else:
            out.append(s)
    return tuple(out)


def _code(template: WorkflowTemplate) -> str:
    with pytest.raises(TemplateCompileError) as err:
        validate_template(template, agent_specs=AGENT_SPECS)
    return err.value.code


def _stage(id_: str) -> TemplateStage:
    return next(s for s in AGENTIC.stages if s.id == id_)


@pytest.mark.parametrize(("key", "runnable"), [(k, t.runnable) for k, t in TEMPLATES.items()])
def test_every_registered_template_is_structurally_valid_and_compiles_iff_runnable(
    key: tuple[str, str], runnable: bool
) -> None:
    validate_template(TEMPLATES[key], agent_specs=AGENT_SPECS)
    if runnable:
        assert isinstance(compile_template(*key), WorkflowPlan)
    else:
        with pytest.raises(TemplateCompileError) as err:
            compile_template(*key)
        assert err.value.code == "template_not_runnable"


def test_daily_legacy_compiles_to_the_legacy_pipeline() -> None:
    plan = compile_template("daily_legacy", "1")
    assert plan.execution == "legacy_pipeline"
    assert [s.kind for s in plan.stages] == ["existing"] * 5


def test_the_agentic_plan_pins_every_agent_spec_version_and_hash() -> None:
    plan = compile_template("shorts_agentic", "1")
    assert [s.stage_id for s in plan.stages] == [s.id for s in AGENTIC.stages]
    agents = [s for s in (*plan.stages, *plan.post_publish) if s.kind == "agent"]
    assert {s.agent_role for s in agents} == {
        AgentRole.STRATEGY,
        AgentRole.CREATIVE_DIRECTOR,
        AgentRole.QUALITY_REVIEWER,
        AgentRole.LEARNING,
    }
    for s in agents:
        assert s.agent_role is not None and s.agent_spec_version == "1.0"
        spec = AGENT_SPECS[(s.agent_role, "1.0")]
        assert s.agent_spec_hash == spec.manifest_hash
        assert (s.model_policy_ref, s.prompt_version, s.timeout_seconds) == (
            spec.model_policy_ref,
            spec.prompt_version,
            spec.timeout_seconds,
        )
    assert plan.post_publish[0].trigger == "public_metrics_available"


def test_compilation_is_deterministic_and_the_hash_covers_the_agent_specs() -> None:
    a, b = compile_template("shorts_agentic", "1"), compile_template("shorts_agentic", "1")
    assert a == b and a.plan_hash == b.plan_hash
    strategy_key = (AgentRole.STRATEGY, "1.0")
    changed = dict(AGENT_SPECS)
    changed[strategy_key] = dataclasses.replace(
        AGENT_SPECS[strategy_key], max_estimated_cost_usd=Decimal("0.99")
    )
    other = compile_template("shorts_agentic", "1", agent_specs=changed)
    assert other.plan_hash != a.plan_hash
    assert other.template_hash == a.template_hash


def test_a_tampered_plan_is_rejected_by_its_own_hash() -> None:
    data = compile_template("shorts_agentic", "1").model_dump(mode="json")
    data["max_qa_repair_cycles"] = 2
    with pytest.raises(ValueError, match="plan_hash"):
        WorkflowPlan.model_validate(data)


def test_output_order_is_topological_with_declaration_order_for_ties() -> None:
    s = AGENTIC.stages
    shuffled = _tpl(stages=tuple(reversed(s)))
    plan = compile_template("t", "1", templates={("t", "1"): shuffled})
    ids = [p.stage_id for p in plan.stages]
    assert ids == [x.id for x in s]  # 鎖なので一意に決まる


def test_ties_keep_declaration_order() -> None:
    t = _tpl(
        stages=(
            _stage("strategy"),
            _stage("producer"),
            dataclasses.replace(_stage("script"), after=("producer",)),
            dataclasses.replace(_stage("creative"), after=("producer",)),
            dataclasses.replace(_stage("storyboard"), after=("script", "creative")),
            *[
                x
                for x in AGENTIC.stages
                if x.id in ("production", "render", "private_upload", "quality")
            ],
        )
    )
    plan = compile_template("t", "1", templates={("t", "1"): t})
    ids = [p.stage_id for p in plan.stages]
    assert ids.index("script") < ids.index("creative")  # 宣言順


# --- 1 error code = 1 mutation ---------------------------------------------------------


def test_unknown_template() -> None:
    with pytest.raises(TemplateCompileError) as err:
        compile_template("nope", "1")
    assert err.value.code == "unknown_template"
    with pytest.raises(TemplateCompileError) as err:
        compile_template("shorts_agentic", "9")
    assert err.value.code == "unknown_template"


def test_unknown_handler() -> None:
    bad = dataclasses.replace(_stage("script"), handler="run_arbitrary_code")
    assert _code(_tpl(stages=_stages(script=bad))) == "unknown_handler"


def test_unknown_agent_spec() -> None:
    assert (
        _code(
            _tpl(
                stages=_stages(
                    strategy=dataclasses.replace(_stage("strategy"), agent_ref="strategy@9.9")
                )
            )
        )
        == "unknown_agent_spec"
    )
    assert (
        _code(
            _tpl(stages=_stages(strategy=dataclasses.replace(_stage("strategy"), agent_ref=None)))
        )
        == "unknown_agent_spec"
    )
    assert (
        _code(
            _tpl(
                stages=_stages(
                    strategy=dataclasses.replace(_stage("strategy"), agent_ref="producer@1.0")
                )
            )
        )
        == "unknown_agent_spec"
    )


def test_handler_mismatch() -> None:
    swapped = dataclasses.replace(_stage("strategy"), agent_ref="learning@1.0")
    assert _code(_tpl(stages=_stages(strategy=swapped))) == "handler_mismatch"
    existing_with_agent = dataclasses.replace(_stage("script"), agent_ref="strategy@1.0")
    assert _code(_tpl(stages=_stages(script=existing_with_agent))) == "handler_mismatch"


def test_duplicate_stage() -> None:
    dup = dataclasses.replace(_stage("render"), id="script")
    assert _code(_tpl(stages=(*AGENTIC.stages, dup))) == "duplicate_stage"
    in_post = dataclasses.replace(AGENTIC.post_publish[0], id="strategy")
    assert _code(_tpl(post_publish=(in_post,))) == "duplicate_stage"


def test_unknown_dependency() -> None:
    bad = dataclasses.replace(_stage("render"), after=("production", "ghost"))
    assert _code(_tpl(stages=_stages(render=bad))) == "unknown_dependency"
    bad_post = dataclasses.replace(AGENTIC.post_publish[0], after=("ghost",))
    assert _code(_tpl(post_publish=(bad_post,))) == "unknown_dependency"
    # 本編 stage は post_publish に依存できない
    on_post = dataclasses.replace(_stage("quality"), after=("lesson",))
    assert _code(_tpl(stages=_stages(quality=on_post))) == "unknown_dependency"


def test_cycle() -> None:
    loop = dataclasses.replace(_stage("strategy"), after=("quality",))
    assert _code(_tpl(stages=_stages(strategy=loop))) == "cycle"
    self_loop = dataclasses.replace(_stage("render"), after=("render",))
    assert _code(_tpl(stages=_stages(render=self_loop))) == "cycle"


@pytest.mark.parametrize(
    "missing", ["script", "storyboard", "production", "render", "private_upload"]
)
def test_missing_required_stage(missing: str) -> None:
    stages = [s for s in _stages(**{missing: None})]
    # 後続の after から消えた id を外して、欠落だけを原因にする
    fixed = tuple(
        dataclasses.replace(s, after=tuple(a for a in s.after if a != missing)) for s in stages
    )
    assert _code(_tpl(stages=fixed)) == "missing_required_stage"


def test_a_required_stage_appearing_twice_is_missing_required_stage() -> None:
    again = dataclasses.replace(_stage("render"), id="render2", after=("render",))
    assert _code(_tpl(stages=(*AGENTIC.stages, again))) == "missing_required_stage"


def test_required_stage_order() -> None:
    # render が production に依存しない（並列化）= 順序違反
    parallel = dataclasses.replace(_stage("render"), after=("storyboard",))
    upload = dataclasses.replace(_stage("private_upload"), after=("render",))
    prod = dataclasses.replace(_stage("production"), after=("storyboard",))
    t = _tpl(stages=_stages(render=parallel, private_upload=upload, production=prod))
    assert _code(t) == "required_stage_order"


def test_strategy_without_producer() -> None:
    # producer を外し、creative を strategy に直接つなぐ
    creative = dataclasses.replace(_stage("creative"), after=("strategy",))
    t = _tpl(stages=_stages(producer=None, creative=creative))
    assert _code(t) == "strategy_without_producer"
    # producer があっても strategy に依存していなければ同じ
    detached = dataclasses.replace(_stage("producer"), after=())
    assert _code(_tpl(stages=_stages(producer=detached))) == "strategy_without_producer"


@pytest.mark.parametrize("cycles", [3, 99, -1])
def test_qa_cycle_limit(cycles: int) -> None:
    assert _code(_tpl(max_qa_repair_cycles=cycles)) == "qa_cycle_limit"


def test_invalid_post_publish() -> None:
    not_learning = dataclasses.replace(
        AGENTIC.post_publish[0],
        id="x",
        handler="quality_reviewer_v1",
        agent_ref="quality_reviewer@1.0",
    )
    assert _code(_tpl(post_publish=(not_learning,))) == "invalid_post_publish"
    no_trigger = dataclasses.replace(AGENTIC.post_publish[0], trigger=None)
    assert _code(_tpl(post_publish=(no_trigger,))) == "invalid_post_publish"
    existing = TemplateStage("late", "existing_render", (), trigger="t")
    assert _code(_tpl(post_publish=(existing,))) == "invalid_post_publish"
