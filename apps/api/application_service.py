"""route が依存する Application Service の型と、本物の組み立て（ADR-0044 §2/§4）。

route は ``ApplicationServicePort`` しか知らない。scope・人限定の検査と観測は service が行う。
テストは ``get_application_service`` を fake で override する。
"""

from __future__ import annotations

import asyncio
import uuid
import weakref
from collections.abc import Awaitable, Callable
from typing import Annotated, Protocol

from fastapi import Depends
from temporalio.client import Client

from application.ports import AgenticWorkflowStarter, Caller
from apps.api.application_adapters import BackendCommandPort, LoggingOperationObserver
from apps.api.dependencies import get_artifact_store_factory, get_session_factory, get_settings
from apps.api.routers.intervention import Backend
from contracts.application import (
    CreateEpisodeDraftRequest,
    EpisodeDraftResult,
    ProposeStoryboardChangeRequest,
    StartAgenticProductionResult,
    StartedWorkflow,
    StoryboardProposalResult,
    WorkerCheck,
)
from contracts.application_operations import (
    ApplyApprovedChangeRequest,
    ChangeImpactPreview,
    CommandAccepted,
    EpisodeList,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    ImprovementTrace,
    ListEpisodesRequest,
    ListToolCallsRequest,
    RequestHoldRequest,
    ResumeAccepted,
    ResumeProductionRequest,
    ToolCallDetail,
    ToolCallList,
    WorkflowProgress,
)
from infrastructure.config import Settings


class ApplicationServicePort(Protocol):
    """``application.service.ApplicationService`` の公開面（16 操作）。"""

    async def create_episode_draft(
        self, caller: Caller, request: CreateEpisodeDraftRequest
    ) -> EpisodeDraftResult: ...

    async def list_episodes(self, caller: Caller, request: ListEpisodesRequest) -> EpisodeList: ...

    async def get_episode_workspace(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> EpisodeWorkspace: ...

    async def start_agentic_production(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> StartAgenticProductionResult: ...

    async def request_hold(
        self, caller: Caller, episode_id: uuid.UUID, request: RequestHoldRequest
    ) -> CommandAccepted: ...

    async def resume_production(
        self, caller: Caller, episode_id: uuid.UUID, request: ResumeProductionRequest
    ) -> ResumeAccepted: ...

    async def get_storyboard(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> GetStoryboardResult: ...

    async def propose_storyboard_change(
        self, caller: Caller, episode_id: uuid.UUID, request: ProposeStoryboardChangeRequest
    ) -> StoryboardProposalResult: ...

    async def preview_change_impact(
        self, caller: Caller, episode_id: uuid.UUID, change_set_id: str
    ) -> ChangeImpactPreview: ...

    async def apply_approved_change(
        self,
        caller: Caller,
        episode_id: uuid.UUID,
        change_set_id: str,
        request: ApplyApprovedChangeRequest,
    ) -> CommandAccepted: ...

    async def get_agent_decisions(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> GetAgentDecisionsResult: ...

    async def get_quality_review(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> GetQualityReviewResult: ...

    async def get_workflow_progress(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> WorkflowProgress: ...

    async def list_tool_calls(
        self, caller: Caller, episode_id: uuid.UUID, request: ListToolCallsRequest
    ) -> ToolCallList: ...

    async def get_tool_call(
        self, caller: Caller, episode_id: uuid.UUID, tool_call_id: str
    ) -> ToolCallDetail: ...

    async def get_improvement_trace(
        self, caller: Caller, episode_id: uuid.UUID
    ) -> ImprovementTrace: ...


class LazyAgenticStarter:
    """Temporal へは最初に必要になったときだけ繋ぐ（読み取りの要求で接続しない）。

    options は worker・CLI と同じ ``pipeline_options_from_settings``（ADR-0044 §3-2）。
    """

    def __init__(
        self,
        settings: Settings,
        connect: Callable[[Settings], Awaitable[Client]] | None = None,
    ) -> None:
        self._settings = settings
        self._connect = connect
        self._inner: AgenticWorkflowStarter | None = None
        self._lock = asyncio.Lock()

    async def _starter(self) -> AgenticWorkflowStarter:
        async with self._lock:
            return await self._connected()

    async def _connected(self) -> AgenticWorkflowStarter:
        if self._inner is None:
            from infrastructure.temporal.agentic_starter import TemporalAgenticStarter
            from infrastructure.temporal.pipeline_options import pipeline_options_from_settings

            client = await (self._connect or _connect_temporal)(self._settings)
            self._inner = TemporalAgenticStarter(
                client, pipeline_options_from_settings(self._settings)
            )
        return self._inner

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow:
        return await (await self._starter()).start(
            episode_id=episode_id, template_id=template_id, template_version=template_version
        )

    async def probe_workers(self) -> WorkerCheck:
        return await (await self._starter()).probe_workers()


#: event loop ごとに1つ（要求ごとに Temporal へ新しい接続を作らない。Temporal の client は作った
#: loop でしか使えないので loop を跨いで使い回さない。loop が消えれば行も消える）
_LOOP_STARTERS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, LazyAgenticStarter] = (
    weakref.WeakKeyDictionary()
)


def loop_starter(settings: Settings) -> LazyAgenticStarter:
    """実行中の event loop の starter（無ければ作る）。async の文脈から呼ぶ。"""
    loop = asyncio.get_running_loop()
    starter = _LOOP_STARTERS.get(loop)
    if starter is None:
        starter = _LOOP_STARTERS[loop] = LazyAgenticStarter(settings)
    return starter


async def _connect_temporal(settings: Settings) -> Client:  # pragma: no cover - 実接続経路
    from infrastructure.temporal.connect import connect_with_retry

    return await connect_with_retry(settings, max_attempts=3)


async def get_application_service(
    backend: Backend, settings: Annotated[Settings, Depends(get_settings)]
) -> ApplicationServicePort:  # pragma: no cover - 実接続経路
    """本物の組み立て。import は関数内（Temporal・MinIO の依存を route の import に入れない）。"""
    from application.service import ApplicationService
    from infrastructure.agents.draft_port import DbEpisodeDraftPort
    from infrastructure.application.episode_reader import StoreBackedEpisodeReader
    from infrastructure.application.tool_evidence import StoreBackedToolEvidenceReader

    session_factory = get_session_factory()
    store_factory = get_artifact_store_factory()
    return ApplicationService(
        commands=BackendCommandPort(backend),
        reads=StoreBackedEpisodeReader(session_factory, store_factory),
        drafts=DbEpisodeDraftPort(
            session_factory=session_factory, store=store_factory(), bucket=settings.minio_bucket
        ),
        starter=loop_starter(settings),
        agentic_enabled=settings.jpmas2_agentic_workflows,
        agent_tools_enabled=settings.jpmas2_agent_tools,
        observer=LoggingOperationObserver(),
        tool_evidence=(
            StoreBackedToolEvidenceReader(session_factory, store_factory)
            if settings.jpmas2_agent_tools
            else None
        ),
    )
