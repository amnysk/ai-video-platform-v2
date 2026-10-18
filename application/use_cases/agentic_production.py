"""``CreateEpisodeDraft`` / ``StartAgenticProduction``（ADR-0043 §6）。所有者の CLI が呼ぶ。

- draft: Episode・UserBrief・``episode_controls(mode=auto_intervenable)`` を作るだけ。
  生成を始めない。日次の枠・TopicPlan を使わない。template は存在・runnable・agentic でなければ
  作らない。
- start: flag ``jpmas2_agentic_workflows`` が有効なときだけ ``AgenticEpisodeWorkflow`` を起動する。
  flag は構築時に渡す（application は infrastructure を import しない / INV-6）。
"""

from __future__ import annotations

from application.errors import OperationError
from application.ports import AgenticWorkflowStarter, EpisodeDraftPort
from contracts.agentic import UserBrief, WorkflowPlan
from contracts.application import (
    CreateEpisodeDraftRequest,
    EpisodeDraftResult,
    OperationErrorCode,
    StartAgenticProductionRequest,
    StartAgenticProductionResult,
)
from contracts.topic import TOPIC_MAX_CHARS
from domain.workflow_templates.compiler import (
    TemplateCompileError,
    compile_agentic_template,
    operation_code_for,
    plan_requires_agent_tools,
)


def compile_for_agentic_run(brief: UserBrief) -> WorkflowPlan:
    """brief の template が agentic で実行できるか確かめる（draft と start で同じ検査）。"""
    try:
        plan = compile_agentic_template(brief.template_id, brief.template_version)
    except TemplateCompileError as exc:
        raise OperationError(
            operation_code_for(exc),
            f"template {brief.template_id}@{brief.template_version} cannot run: {exc.detail}",
            {"template_id": brief.template_id, "template_version": brief.template_version},
        ) from exc
    if brief.format_profile != plan.format_profile:
        raise OperationError(
            OperationErrorCode.INVALID_PAYLOAD,
            f"format_profile {brief.format_profile!r} does not match the template's "
            f"{plan.format_profile!r}",
        )
    return plan


class CreateEpisodeDraft:
    def __init__(self, drafts: EpisodeDraftPort) -> None:
        self._drafts = drafts

    async def execute(self, request: CreateEpisodeDraftRequest) -> EpisodeDraftResult:
        compile_for_agentic_run(request.brief)
        if len(request.brief.theme) > TOPIC_MAX_CHARS:
            raise OperationError(
                OperationErrorCode.INVALID_PAYLOAD,
                f"theme is longer than {TOPIC_MAX_CHARS} characters",
            )
        record = await self._drafts.create_draft(command_id=request.command_id, brief=request.brief)
        return EpisodeDraftResult(
            command_id=request.command_id, episode_id=record.episode_id, created=record.created
        )


class StartAgenticProduction:
    def __init__(
        self,
        drafts: EpisodeDraftPort,
        starter: AgenticWorkflowStarter,
        *,
        enabled: bool,
        agent_tools_enabled: bool = False,
    ) -> None:
        self._drafts = drafts
        self._starter = starter
        self._enabled = enabled
        #: ``JPMAS2_AGENT_TOOLS``（ADR-0047）。Agent Tool を使う計画は、これが無いと起動しない
        self._agent_tools_enabled = agent_tools_enabled

    async def execute(self, request: StartAgenticProductionRequest) -> StartAgenticProductionResult:
        if not self._enabled:
            raise OperationError(
                OperationErrorCode.FEATURE_DISABLED,
                "agentic workflows are disabled (JPMAS2_AGENTIC_WORKFLOWS=false)",
            )
        brief = await self._drafts.get_brief(request.episode_id)
        if brief is None:
            raise OperationError(
                OperationErrorCode.EPISODE_NOT_FOUND,
                f"episode {request.episode_id} has no draft (UserBrief)",
            )
        plan = compile_for_agentic_run(brief)
        if plan_requires_agent_tools(plan) and not self._agent_tools_enabled:
            raise OperationError(
                OperationErrorCode.FEATURE_DISABLED,
                f"template {brief.template_id}@{brief.template_version} uses agent tools, which "
                "are disabled (JPMAS2_AGENT_TOOLS=false)",
                {"template_id": brief.template_id, "template_version": brief.template_version},
            )
        started = await self._starter.start(
            episode_id=request.episode_id,
            template_id=brief.template_id,
            template_version=brief.template_version,
        )
        return StartAgenticProductionResult(
            episode_id=request.episode_id,
            workflow_id=started.workflow_id,
            started=started.started,
            worker_check=started.worker_check,
        )
