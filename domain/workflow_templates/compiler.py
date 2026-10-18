"""Workflow Template の静的検査とコンパイル（ADR-0043 §3、INV-53）。純粋関数。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from contracts.agentic import PlannedStage, WorkflowPlan, compute_plan_hash
from contracts.agents import (
    AGENT_SPECS,
    AgentHandlerRef,
    AgentSpec,
    UnknownAgentSpecError,
    parse_agent_ref,
    resolve_agent_spec,
)
from contracts.application import OperationErrorCode
from contracts.intervention import AgentRole
from contracts.workflow_templates import (
    HARD_MAX_QA_REPAIR_CYCLES,
    REQUIRED_STAGE_ORDER,
    TEMPLATES,
    ExistingStageHandler,
    TemplateStage,
    WorkflowTemplate,
    template_hash,
)

_AGENT_HANDLERS = {h.value for h in AgentHandlerRef}
_EXISTING_HANDLERS = {h.value for h in ExistingStageHandler}


class TemplateCompileError(Exception):
    """``code`` は機械可読（INV-53 のテストが列挙する）。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _agent_spec_for(
    stage: TemplateStage, agent_specs: Mapping[tuple[AgentRole, str], AgentSpec]
) -> AgentSpec | None:
    """agent stage なら spec を返す。existing stage は None。不整合は例外。"""
    if stage.handler in _EXISTING_HANDLERS:
        if stage.agent_ref is not None:
            raise TemplateCompileError(
                "handler_mismatch", f"existing stage {stage.id!r} must not carry agent_ref"
            )
        return None
    if stage.agent_ref is None:
        raise TemplateCompileError(
            "unknown_agent_spec", f"agent stage {stage.id!r} has no agent_ref"
        )
    try:
        role, version = parse_agent_ref(stage.agent_ref)
        spec = resolve_agent_spec(role, version, specs=dict(agent_specs))
    except UnknownAgentSpecError as exc:
        raise TemplateCompileError("unknown_agent_spec", f"{stage.id}: {exc}") from exc
    if spec.handler_ref.value != stage.handler:
        raise TemplateCompileError(
            "handler_mismatch",
            f"stage {stage.id!r}: handler {stage.handler!r} != spec handler "
            f"{spec.handler_ref.value!r}",
        )
    return spec


def _toposort(stages: Sequence[TemplateStage]) -> list[TemplateStage]:
    """宣言順で安定な位相順。循環は ``cycle``。"""
    done: set[str] = set()
    remaining = list(stages)
    ordered: list[TemplateStage] = []
    while remaining:
        ready = next((s for s in remaining if all(a in done for a in s.after)), None)
        if ready is None:
            raise TemplateCompileError("cycle", f"stages form a cycle: {[s.id for s in remaining]}")
        ordered.append(ready)
        done.add(ready.id)
        remaining.remove(ready)
    return ordered


def _ancestors(stage_id: str, by_id: Mapping[str, TemplateStage]) -> set[str]:
    seen: set[str] = set()
    todo = list(by_id[stage_id].after)
    while todo:
        current = todo.pop()
        if current not in seen:
            seen.add(current)
            todo.extend(by_id[current].after)
    return seen


def validate_template(
    template: WorkflowTemplate,
    *,
    agent_specs: Mapping[tuple[AgentRole, str], AgentSpec],
) -> None:
    if not 0 <= template.max_qa_repair_cycles <= HARD_MAX_QA_REPAIR_CYCLES:
        raise TemplateCompileError(
            "qa_cycle_limit",
            f"max_qa_repair_cycles {template.max_qa_repair_cycles} not in "
            f"0..{HARD_MAX_QA_REPAIR_CYCLES}",
        )
    everything = (*template.stages, *template.post_publish)
    for stage in everything:
        if stage.handler not in _AGENT_HANDLERS | _EXISTING_HANDLERS:
            raise TemplateCompileError(
                "unknown_handler", f"stage {stage.id!r}: unknown handler {stage.handler!r}"
            )
    ids = [s.id for s in everything]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        raise TemplateCompileError("duplicate_stage", f"duplicate stage ids: {duplicated}")
    for stage in everything:
        _agent_spec_for(stage, agent_specs)
    main_ids = {s.id for s in template.stages}
    for stage in template.stages:
        for dep in stage.after:
            if dep not in main_ids:
                raise TemplateCompileError(
                    "unknown_dependency", f"stage {stage.id!r} depends on unknown {dep!r}"
                )
    for stage in template.post_publish:
        for dep in stage.after:
            if dep not in main_ids:
                raise TemplateCompileError(
                    "unknown_dependency", f"post_publish {stage.id!r} depends on unknown {dep!r}"
                )
        if stage.handler != AgentHandlerRef.LEARNING_AGENT_V1.value or not stage.trigger:
            raise TemplateCompileError(
                "invalid_post_publish",
                f"post_publish {stage.id!r} must be learning_agent_v1 with a trigger",
            )
    _toposort(template.stages)
    by_id = {s.id: s for s in template.stages}
    if template.execution == "legacy_pipeline":
        return
    required_ids: list[str] = []
    for handler in REQUIRED_STAGE_ORDER:
        found = [s.id for s in template.stages if s.handler == handler.value]
        if len(found) != 1:
            raise TemplateCompileError(
                "missing_required_stage",
                f"required stage {handler.value!r} must appear exactly once (found {len(found)})",
            )
        required_ids.append(found[0])
    for earlier, later in zip(required_ids, required_ids[1:], strict=False):
        if earlier not in _ancestors(later, by_id):
            raise TemplateCompileError(
                "required_stage_order", f"{later!r} must (transitively) come after {earlier!r}"
            )
    strategy_ids = [
        s.id for s in template.stages if s.handler == AgentHandlerRef.STRATEGY_AGENT_V1.value
    ]
    producers = [
        s for s in template.stages if s.handler == ExistingStageHandler.PRODUCER_ADOPT.value
    ]
    for strategy_id in strategy_ids:
        if not any(strategy_id in _ancestors(p.id, by_id) for p in producers):
            raise TemplateCompileError(
                "strategy_without_producer",
                f"strategy stage {strategy_id!r} needs an existing_producer_adopt stage after it",
            )


def _planned(
    stage: TemplateStage, agent_specs: Mapping[tuple[AgentRole, str], AgentSpec]
) -> PlannedStage:
    spec = _agent_spec_for(stage, agent_specs)
    if spec is None:
        return PlannedStage(
            stage_id=stage.id,
            handler=stage.handler,
            kind="existing",
            after=stage.after,
            trigger=stage.trigger,
        )
    return PlannedStage(
        stage_id=stage.id,
        handler=stage.handler,
        kind="agent",
        after=stage.after,
        agent_role=spec.role,
        agent_spec_version=spec.spec_version,
        agent_spec_hash=spec.manifest_hash,
        model_policy_ref=spec.model_policy_ref,
        prompt_version=spec.prompt_version,
        timeout_seconds=spec.timeout_seconds,
        trigger=stage.trigger,
    )


def compile_template(
    template_id: str,
    version: str,
    *,
    templates: Mapping[tuple[str, str], WorkflowTemplate] = TEMPLATES,
    agent_specs: Mapping[tuple[AgentRole, str], AgentSpec] | None = None,
) -> WorkflowPlan:
    if agent_specs is None:
        agent_specs = AGENT_SPECS
    template = templates.get((template_id, version))
    if template is None:
        raise TemplateCompileError("unknown_template", f"{template_id}@{version}")
    if not template.runnable:
        raise TemplateCompileError("template_not_runnable", f"{template_id}@{version}")
    validate_template(template, agent_specs=agent_specs)
    fields: dict[str, object] = {
        "type": "workflow_plan",
        "schema_version": "1.0",
        "template_id": template.template_id,
        "template_version": template.version,
        "template_hash": template_hash(template),
        "format_profile": template.format_profile,
        "execution": template.execution,
        "stages": [
            _planned(s, agent_specs).model_dump(mode="json") for s in _toposort(template.stages)
        ],
        "post_publish": [
            _planned(s, agent_specs).model_dump(mode="json") for s in template.post_publish
        ],
        "max_qa_repair_cycles": template.max_qa_repair_cycles,
    }
    return WorkflowPlan.model_validate({**fields, "plan_hash": compute_plan_hash(fields)})


def plan_requires_agent_tools(
    plan: WorkflowPlan,
    *,
    agent_specs: Mapping[tuple[AgentRole, str], AgentSpec] | None = None,
) -> bool:
    """計画のどれかの agent stage が Agent Tool を使う spec を固定しているか。

    ``True`` の計画は ``JPMAS2_AGENT_TOOLS`` が有効なときだけ起動できる（ADR-0047 §2 / §9）。
    """
    specs = AGENT_SPECS if agent_specs is None else agent_specs
    for stage in (*plan.stages, *plan.post_publish):
        if stage.kind != "agent" or stage.agent_role is None or stage.agent_spec_version is None:
            continue
        spec = specs.get((stage.agent_role, stage.agent_spec_version))
        if spec is not None and spec.allowed_tool_ids:
            return True
    return False


#: コンパイルの失敗 code -> 操作の失敗 code。ここに無い code は定義の誤り（``invalid_payload``）
_OPERATION_CODES: dict[str, OperationErrorCode] = {
    "unknown_template": OperationErrorCode.UNKNOWN_TEMPLATE,
    "template_not_runnable": OperationErrorCode.TEMPLATE_NOT_RUNNABLE,
    "unknown_agent_spec": OperationErrorCode.UNKNOWN_AGENT_SPEC,
}


def operation_code_for(error: TemplateCompileError) -> OperationErrorCode:
    return _OPERATION_CODES.get(error.code, OperationErrorCode.INVALID_PAYLOAD)


def compile_agentic_template(
    template_id: str,
    version: str,
    *,
    templates: Mapping[tuple[str, str], WorkflowTemplate] = TEMPLATES,
    agent_specs: Mapping[tuple[AgentRole, str], AgentSpec] | None = None,
) -> WorkflowPlan:
    """agentic 実行のための入口。``execution=legacy_pipeline``（``daily_legacy``）は拒否する。

    日次の legacy pipeline は ``EpisodePipelineWorkflow`` が走らせる。これで agentic workflow を
    起動しない（``template_not_runnable``）。
    """
    plan = compile_template(template_id, version, templates=templates, agent_specs=agent_specs)
    if plan.execution != "agentic":
        raise TemplateCompileError(
            "template_not_runnable",
            f"{template_id}@{version} is a legacy pipeline template; it is not run by the "
            "agentic workflow",
        )
    return plan
