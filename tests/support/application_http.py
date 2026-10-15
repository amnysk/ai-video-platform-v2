"""P2-A の HTTP / MCP 並びのテストが共有する fake service とサンプル。

理由は docs/testing/jpmas2-p2a-http-rationale.md。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from application.errors import OperationError
from application.ports import Caller
from apps.api.auth import parse_principals, token_sha256
from contracts.agentic import UserBrief
from contracts.agentic_pipeline import PipelineKind
from contracts.application import (
    EpisodeDraftResult,
    StartAgenticProductionResult,
    StoryboardProposalResult,
)
from contracts.application_operations import (
    OPERATIONS_BY_NAME,
    ApplyApprovedChangeRequest,
    ChangeImpactPreview,
    CommandAccepted,
    EpisodeList,
    EpisodeSummary,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    OperationName,
    OperationSpec,
    RequestHoldRequest,
    ResumeAccepted,
    ResumeProductionRequest,
    WorkflowProgress,
)
from contracts.intervention import CommandStatus, Scope

EPISODE = uuid.UUID("11111111-1111-4111-8111-111111111111")
CHANGE_SET = "cs-0001"
TOKEN = "p2a-owner-token-0001"
NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

#: 全 scope を持つ人の principal だけを登録した registry の JSON
PRINCIPALS_JSON = json.dumps(
    {
        token_sha256(TOKEN): {
            "actor_type": "human",
            "actor_id": "owner",
            "scopes": [s.value for s in Scope],
        }
    }
)


def registry() -> Any:
    return parse_principals(PRINCIPALS_JSON)


AUTH = {"Authorization": f"Bearer {TOKEN}"}

_SUMMARY = EpisodeSummary(
    episode_id=EPISODE,
    status="draft",
    topic=None,
    pipeline_kind=PipelineKind.AGENTIC,
    created_at=NOW,
    updated_at=NOW,
)

#: 操作ごとの HTTP 本文
BODIES: dict[OperationName, dict[str, Any]] = {
    OperationName.CREATE_EPISODE_DRAFT: {
        "command_id": "draft-cmd-0001",
        "brief": UserBrief(theme="theme", template_id="t", template_version="1").model_dump(
            mode="json"
        ),
    },
    OperationName.REQUEST_HOLD: {"command_id": "hold-cmd-0001", "reason": "check"},
    OperationName.RESUME_PRODUCTION: {"command_id": "resume-cmd-0001"},
    OperationName.APPLY_APPROVED_CHANGE: {
        "command_id": "apply-cmd-0001",
        "approved_max_cost_usd": 1.5,
    },
    OperationName.PROPOSE_STORYBOARD_CHANGE: {
        "command_id": "propose-cmd-0001",
        "proposal": {
            "episode_id": str(EPISODE),
            "base_storyboard_revision_id": "rev-1",
            "edits": [{"scene_id": "s1", "visual_description": "a long enough visual description"}],
        },
    },
}


def _accepted(created: bool) -> CommandAccepted:
    return CommandAccepted(
        episode_id=EPISODE, command_id="c-0000001", status=CommandStatus.RECEIVED, created=created
    )


def results(created: bool = True) -> dict[OperationName, BaseModel]:
    """操作ごとの service の戻り値（``created`` を切り替えられるものは切り替える）。"""
    from contracts.application import ProposedSceneCommand

    return {
        OperationName.CREATE_EPISODE_DRAFT: EpisodeDraftResult(
            command_id="draft-cmd-0001", episode_id=EPISODE, created=created
        ),
        OperationName.LIST_EPISODES: EpisodeList(episodes=(_SUMMARY,), next_before=None),
        OperationName.GET_EPISODE_WORKSPACE: EpisodeWorkspace(
            episode=_SUMMARY,
            workflow_id=f"agentic-episode-{EPISODE}",
            control_revision=2,
            pending_change_sets=(),
            read_model={"k": "v"},
        ),
        OperationName.START_AGENTIC_PRODUCTION: StartAgenticProductionResult(
            episode_id=EPISODE, workflow_id=f"agentic-episode-{EPISODE}", started=created
        ),
        OperationName.REQUEST_HOLD: _accepted(created),
        OperationName.RESUME_PRODUCTION: ResumeAccepted(
            episode_id=EPISODE,
            command_id="c-0000002",
            status=CommandStatus.RECEIVED,
            created=created,
            pipeline_kind=PipelineKind.AGENTIC,
        ),
        OperationName.GET_STORYBOARD: GetStoryboardResult(episode_id=EPISODE, storyboard=None),
        OperationName.PROPOSE_STORYBOARD_CHANGE: StoryboardProposalResult(
            request_command_id="propose-cmd-0001",
            episode_id=EPISODE,
            commands=(
                ProposedSceneCommand(
                    scene_id="s1",
                    command_id="c-0000003",
                    status=CommandStatus.RECEIVED,
                    created=created,
                ),
            ),
        ),
        OperationName.PREVIEW_CHANGE_IMPACT: ChangeImpactPreview(
            episode_id=EPISODE,
            change_set_id=CHANGE_SET,
            kind="storyboard",
            status="proposed",
            base_revision_id="rev-1",
            base_manifest_id=None,
            proposal={},
            impact=None,
            estimated_cost_usd=None,
            cost_estimated=False,
            control_revision=2,
            blockers=("cost_not_estimated",),
        ),
        OperationName.APPLY_APPROVED_CHANGE: _accepted(created),
        OperationName.GET_AGENT_DECISIONS: GetAgentDecisionsResult(
            episode_id=EPISODE, decisions=(), agent_runs=()
        ),
        OperationName.GET_QUALITY_REVIEW: GetQualityReviewResult(
            episode_id=EPISODE, status="not_reviewed", review=None, artifact_id=None, findings=()
        ),
        OperationName.GET_WORKFLOW_PROGRESS: WorkflowProgress(
            episode_id=EPISODE,
            episode_status="draft",
            pipeline_kind=PipelineKind.AGENTIC,
            workflow_id=f"agentic-episode-{EPISODE}",
            jobs=(),
            agent_runs=(),
            failure=None,
        ),
    }


def url(spec: OperationSpec) -> str:
    return "/v1/intervention" + spec.http_path.format(episode_id=EPISODE, change_set_id=CHANGE_SET)


@dataclass
class FakeApplicationService:
    """``ApplicationServicePort`` を満たす記録つき fake。``raises`` で操作ごとに失敗させる。"""

    created: bool = True
    raises: dict[OperationName, OperationError] = field(default_factory=dict)
    calls: list[tuple[OperationName, tuple[Any, ...]]] = field(default_factory=list)

    async def _do(self, name: OperationName, caller: Caller, *args: Any) -> Any:
        self.calls.append((name, (caller, *args)))
        if name in self.raises:
            raise self.raises[name]
        return results(self.created)[name]

    async def create_episode_draft(self, caller: Caller, request: Any) -> Any:
        return await self._do(OperationName.CREATE_EPISODE_DRAFT, caller, request)

    async def list_episodes(self, caller: Caller, request: Any) -> Any:
        return await self._do(OperationName.LIST_EPISODES, caller, request)

    async def get_episode_workspace(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.GET_EPISODE_WORKSPACE, caller, episode_id)

    async def start_agentic_production(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.START_AGENTIC_PRODUCTION, caller, episode_id)

    async def request_hold(
        self, caller: Caller, episode_id: uuid.UUID, request: RequestHoldRequest
    ) -> Any:
        return await self._do(OperationName.REQUEST_HOLD, caller, episode_id, request)

    async def resume_production(
        self, caller: Caller, episode_id: uuid.UUID, request: ResumeProductionRequest
    ) -> Any:
        return await self._do(OperationName.RESUME_PRODUCTION, caller, episode_id, request)

    async def get_storyboard(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.GET_STORYBOARD, caller, episode_id)

    async def propose_storyboard_change(
        self, caller: Caller, episode_id: uuid.UUID, request: Any
    ) -> Any:
        return await self._do(OperationName.PROPOSE_STORYBOARD_CHANGE, caller, episode_id, request)

    async def preview_change_impact(
        self, caller: Caller, episode_id: uuid.UUID, change_set_id: str
    ) -> Any:
        return await self._do(
            OperationName.PREVIEW_CHANGE_IMPACT, caller, episode_id, change_set_id
        )

    async def apply_approved_change(
        self,
        caller: Caller,
        episode_id: uuid.UUID,
        change_set_id: str,
        request: ApplyApprovedChangeRequest,
    ) -> Any:
        return await self._do(
            OperationName.APPLY_APPROVED_CHANGE, caller, episode_id, change_set_id, request
        )

    async def get_agent_decisions(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.GET_AGENT_DECISIONS, caller, episode_id)

    async def get_quality_review(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.GET_QUALITY_REVIEW, caller, episode_id)

    async def get_workflow_progress(self, caller: Caller, episode_id: uuid.UUID) -> Any:
        return await self._do(OperationName.GET_WORKFLOW_PROGRESS, caller, episode_id)


def build_app(
    service: FakeApplicationService | None, *, flag: bool = True, auth: bool = True
) -> Any:
    """本物の FastAPI に fake service を差す。``auth=False`` は principal 未設定（503）。"""
    from apps.api.application_service import get_application_service
    from apps.api.dependencies import get_settings
    from apps.api.main import create_app
    from apps.api.routers import intervention
    from infrastructure.config import Settings

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(jpmas2_application_api=flag)
    app.dependency_overrides[intervention.get_principal_registry] = lambda: (
        registry() if auth else None
    )
    if service is not None:
        app.dependency_overrides[get_application_service] = lambda: service
    return app


__all__ = [
    "AUTH",
    "BODIES",
    "CHANGE_SET",
    "EPISODE",
    "OPERATIONS_BY_NAME",
    "FakeApplicationService",
    "build_app",
    "results",
    "url",
]
