"""Application Service（ADR-0044 §2）。UI・MCP・CLI・内部 Agent が使う13操作の唯一の入口。

- 権限（scope・人限定）は ``OPERATIONS_BY_NAME`` から引き、``_authorize`` の1か所で検査する。
- 観測（``OperationRecord``）は ``_observed`` の1か所で、1操作につきちょうど1件記録する。
  入力の全文・秘密は載せない（id・code・時間だけ）。
- 書き込みは既存の介入 command（``CommandPort.receive`` → ``start``）。適用ロジックは持たない
  （INV-50）。
- 読み取りは ``EpisodeReadPort``。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TypeVar

from application.errors import OperationError, OperationErrorCode
from application.ports import (
    AgenticWorkflowStarter,
    Caller,
    CommandPort,
    EpisodeDraftPort,
    EpisodeReadPort,
    OperationObserver,
    OperationRecord,
    ReceivedCommand,
)
from application.use_cases.agentic_production import CreateEpisodeDraft, StartAgenticProduction
from application.use_cases.propose_storyboard_change import ProposeStoryboardChange
from contracts.agentic_pipeline import PipelineKind
from contracts.application import (
    CreateEpisodeDraftRequest,
    EpisodeDraftResult,
    ProposeStoryboardChangeRequest,
    StartAgenticProductionRequest,
    StartAgenticProductionResult,
    StoryboardProposalResult,
    WorkerCheck,
)
from contracts.application_operations import (
    OPERATIONS_BY_NAME,
    ApplyApprovedChangeRequest,
    ChangeImpactPreview,
    CommandAccepted,
    EpisodeList,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    ListEpisodesRequest,
    OperationName,
    RequestHoldRequest,
    ResumeAccepted,
    ResumeProductionRequest,
    WorkflowProgress,
)
from contracts.intervention import (
    ActorType,
    ApplyChangePayload,
    CommandEnvelope,
    CommandType,
    RequestHoldPayload,
    ResumePayload,
)

T = TypeVar("T")

#: ``CommandEnvelope.correlation_id`` の最大長
_CORRELATION_MAX = 64


@dataclass(slots=True)
class _Context:
    """1操作の観測に載せる id（操作の途中で分かったものを足す）。"""

    episode_id: str | None = None
    command_id: str | None = None
    workflow_id: str | None = None


class ApplicationService:
    def __init__(
        self,
        *,
        commands: CommandPort,
        reads: EpisodeReadPort,
        drafts: EpisodeDraftPort,
        starter: AgenticWorkflowStarter,
        agentic_enabled: bool,
        observer: OperationObserver | None = None,
    ) -> None:
        self._commands = commands
        self._reads = reads
        self._starter = starter
        self._agentic_enabled = agentic_enabled
        self._observer = observer
        self._create_draft = CreateEpisodeDraft(drafts)
        self._start_production = StartAgenticProduction(drafts, starter, enabled=agentic_enabled)
        self._propose = ProposeStoryboardChange(commands)

    # ------------------------------------------------------------------ 共通（権限・観測）

    @staticmethod
    def _authorize(name: OperationName, caller: Caller) -> None:
        spec = OPERATIONS_BY_NAME[name]
        if spec.required_scope not in caller.scopes:
            raise OperationError(
                OperationErrorCode.MISSING_SCOPE,
                f"this caller lacks the {spec.required_scope.value!r} scope",
                {"required_scope": spec.required_scope.value},
            )
        if spec.human_only and caller.actor_type is not ActorType.HUMAN:
            raise OperationError(
                OperationErrorCode.FORBIDDEN,
                f"{name.value} can only be requested by a human",
                {"reason": "human_only"},
            )

    async def _observed(
        self,
        name: OperationName,
        caller: Caller,
        episode_id: uuid.UUID | None,
        body: Callable[[_Context], Awaitable[T]],
    ) -> T:
        ctx = _Context(episode_id=None if episode_id is None else str(episode_id))
        started = time.monotonic()
        result = "succeeded"
        error_code: str | None = None
        try:
            self._authorize(name, caller)
            return await body(ctx)
        except OperationError as exc:
            result, error_code = "rejected", exc.code.value
            raise
        except BaseException:
            result = "failed"
            raise
        finally:
            if self._observer is not None:
                self._observer.record(
                    OperationRecord(
                        operation=name,
                        actor_type=caller.actor_type,
                        result=result,
                        duration_ms=int((time.monotonic() - started) * 1000),
                        episode_id=ctx.episode_id,
                        correlation_id=caller.correlation_id,
                        command_id=ctx.command_id,
                        workflow_id=ctx.workflow_id,
                        error_code=error_code,
                    )
                )

    async def _send(
        self,
        episode_id: uuid.UUID,
        envelope: CommandEnvelope,
        caller: Caller,
    ) -> ReceivedCommand:
        """command を受け付け、新しく作られたものだけ配送する（commit 後。再送は起動しない）。"""
        [received] = await self._commands.receive(str(episode_id), [envelope], caller)
        if received.created:
            await self._commands.start([received.command_id])
        return received

    @staticmethod
    def _correlation(caller: Caller) -> str | None:
        cid = caller.correlation_id
        return None if cid is None else cid[:_CORRELATION_MAX]

    # ------------------------------------------------------------------ 書き込み（draft・起動）

    async def create_episode_draft(
        self, caller: Caller, request: CreateEpisodeDraftRequest
    ) -> EpisodeDraftResult:
        async def body(ctx: _Context) -> EpisodeDraftResult:
            ctx.command_id = request.command_id
            result = await self._create_draft.execute(request)
            ctx.episode_id = str(result.episode_id)
            return result

        return await self._observed(OperationName.CREATE_EPISODE_DRAFT, caller, None, body)

    async def start_agentic_production(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> StartAgenticProductionResult:
        async def body(ctx: _Context) -> StartAgenticProductionResult:
            result = await self._start_production.execute(
                StartAgenticProductionRequest(episode_id=episode_id)
            )
            ctx.workflow_id = result.workflow_id
            return result

        return await self._observed(
            OperationName.START_AGENTIC_PRODUCTION, caller, episode_id, body
        )

    # ------------------------------------------------------------------ 読み取り

    async def list_episodes(self, caller: Caller, request: ListEpisodesRequest) -> EpisodeList:
        async def body(_: _Context) -> EpisodeList:
            return await self._reads.list_episodes(limit=request.limit, before=request.before)

        return await self._observed(OperationName.LIST_EPISODES, caller, None, body)

    async def get_episode_workspace(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> EpisodeWorkspace:
        async def body(ctx: _Context) -> EpisodeWorkspace:
            result = await self._reads.workspace(episode_id)
            ctx.workflow_id = result.workflow_id
            return result

        return await self._observed(OperationName.GET_EPISODE_WORKSPACE, caller, episode_id, body)

    async def get_storyboard(self, caller: Caller, episode_id: uuid.UUID) -> GetStoryboardResult:
        async def body(_: _Context) -> GetStoryboardResult:
            return await self._reads.storyboard(episode_id)

        return await self._observed(OperationName.GET_STORYBOARD, caller, episode_id, body)

    async def preview_change_impact(
        self, caller: Caller, episode_id: uuid.UUID, change_set_id: str
    ) -> ChangeImpactPreview:
        async def body(_: _Context) -> ChangeImpactPreview:
            return await self._reads.change_impact(episode_id, change_set_id)

        return await self._observed(OperationName.PREVIEW_CHANGE_IMPACT, caller, episode_id, body)

    async def get_agent_decisions(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> GetAgentDecisionsResult:
        async def body(_: _Context) -> GetAgentDecisionsResult:
            return await self._reads.agent_decisions(episode_id)

        return await self._observed(OperationName.GET_AGENT_DECISIONS, caller, episode_id, body)

    async def get_quality_review(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> GetQualityReviewResult:
        async def body(_: _Context) -> GetQualityReviewResult:
            return await self._reads.quality_review(episode_id)

        return await self._observed(OperationName.GET_QUALITY_REVIEW, caller, episode_id, body)

    async def get_workflow_progress(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> WorkflowProgress:
        async def body(ctx: _Context) -> WorkflowProgress:
            result = await self._reads.progress(episode_id)
            ctx.workflow_id = result.workflow_id
            return result

        return await self._observed(OperationName.GET_WORKFLOW_PROGRESS, caller, episode_id, body)

    # ------------------------------------------------------------------ 書き込み（介入 command）

    async def request_hold(
        self, caller: Caller, episode_id: uuid.UUID, request: RequestHoldRequest
    ) -> CommandAccepted:
        async def body(ctx: _Context) -> CommandAccepted:
            ctx.command_id = request.command_id
            payload = RequestHoldPayload(reason=request.reason)
            received = await self._send(
                episode_id,
                CommandEnvelope(
                    command_id=request.command_id,
                    type=CommandType.REQUEST_HOLD,
                    expected_control_revision=request.expected_control_revision,
                    payload=payload.model_dump(mode="json", exclude_none=True),
                    correlation_id=self._correlation(caller),
                ),
                caller,
            )
            return CommandAccepted(
                episode_id=episode_id,
                command_id=received.command_id,
                status=received.status,
                created=received.created,
            )

        return await self._observed(OperationName.REQUEST_HOLD, caller, episode_id, body)

    async def resume_production(
        self, caller: Caller, episode_id: uuid.UUID, request: ResumeProductionRequest
    ) -> ResumeAccepted:
        async def body(ctx: _Context) -> ResumeAccepted:
            ctx.command_id = request.command_id
            kind = await self._reads.pipeline_kind(episode_id)
            agentic = kind is PipelineKind.AGENTIC
            if agentic and not self._agentic_enabled:
                # command を作る前に断る（worker の無い workflow を投入しない / ADR-0044 §3）
                raise OperationError(
                    OperationErrorCode.FEATURE_DISABLED,
                    "agentic workflows are disabled (JPMAS2_AGENTIC_WORKFLOWS=false)",
                )
            payload = ResumePayload(start_stage=request.start_stage)
            received = await self._send(
                episode_id,
                CommandEnvelope(
                    command_id=request.command_id,
                    type=CommandType.RESUME,
                    expected_control_revision=request.expected_control_revision,
                    payload=payload.model_dump(mode="json", exclude_none=True),
                    correlation_id=self._correlation(caller),
                ),
                caller,
            )
            worker_check = WorkerCheck.UNKNOWN
            if agentic:
                # 受付は commit 済み。確認の失敗で応答を失敗にしない（unknown と答える）
                try:
                    worker_check = await self._starter.probe_workers()
                except Exception:  # noqa: BLE001
                    worker_check = WorkerCheck.UNKNOWN
            return ResumeAccepted(
                episode_id=episode_id,
                command_id=received.command_id,
                status=received.status,
                created=received.created,
                pipeline_kind=kind,
                worker_check=worker_check,
            )

        return await self._observed(OperationName.RESUME_PRODUCTION, caller, episode_id, body)

    async def propose_storyboard_change(
        self, caller: Caller, episode_id: uuid.UUID, request: ProposeStoryboardChangeRequest
    ) -> StoryboardProposalResult:
        async def body(ctx: _Context) -> StoryboardProposalResult:
            ctx.command_id = request.command_id
            return await self._propose.execute(caller, episode_id, request)

        return await self._observed(
            OperationName.PROPOSE_STORYBOARD_CHANGE, caller, episode_id, body
        )

    async def apply_approved_change(
        self,
        caller: Caller,
        episode_id: uuid.UUID,
        change_set_id: str,
        request: ApplyApprovedChangeRequest,
    ) -> CommandAccepted:
        async def body(ctx: _Context) -> CommandAccepted:
            ctx.command_id = request.command_id
            preview = await self._reads.change_impact(episode_id, change_set_id)
            _check_applicable(preview, request.approved_max_cost_usd)
            payload = ApplyChangePayload(
                change_set_id=change_set_id,
                max_estimated_cost_usd=request.approved_max_cost_usd,
            )
            received = await self._send(
                episode_id,
                CommandEnvelope(
                    command_id=request.command_id,
                    type=CommandType.APPLY_CHANGE,
                    expected_control_revision=request.expected_control_revision,
                    payload=payload.model_dump(mode="json", exclude_none=True),
                    correlation_id=self._correlation(caller),
                ),
                caller,
            )
            return CommandAccepted(
                episode_id=episode_id,
                command_id=received.command_id,
                status=received.status,
                created=received.created,
            )

        return await self._observed(OperationName.APPLY_APPROVED_CHANGE, caller, episode_id, body)


def _check_applicable(preview: ChangeImpactPreview, approved_max_cost_usd: float) -> None:
    """適用 command を作る前の検査。最終判断は backend の apply（予算 gate 等）が行う。"""
    if preview.status != "proposed":
        raise OperationError(
            OperationErrorCode.INVALID_STATE,
            f"the change set is {preview.status!r}; only a proposed change can be applied",
            {"status": preview.status},
        )
    if not preview.cost_estimated or preview.estimated_cost_usd is None:
        raise OperationError(
            OperationErrorCode.COST_NOT_ESTIMATED,
            "the change set has no cost estimate; it is not applied",
        )
    try:
        estimate = Decimal(preview.estimated_cost_usd)
    except InvalidOperation:
        raise OperationError(
            OperationErrorCode.COST_NOT_ESTIMATED, "the cost estimate is not a number"
        ) from None
    if estimate > Decimal(str(approved_max_cost_usd)):
        raise OperationError(
            OperationErrorCode.BUDGET_EXCEEDED,
            "the estimated cost exceeds the approved maximum",
            {
                "estimated_cost_usd": preview.estimated_cost_usd,
                "approved_max_cost_usd": approved_max_cost_usd,
            },
        )
