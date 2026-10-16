"""ユースケースが使う port（ADR-0043 §1）。実装は apps / infrastructure が構造的に満たす。

実装は backend のエラーを ``OperationError`` に写して投げる。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from contracts.agentic import UserBrief
from contracts.agentic_pipeline import PipelineKind
from contracts.application import DraftRecord, StartedWorkflow, WorkerCheck
from contracts.application_operations import (
    ChangeImpactPreview,
    EpisodeList,
    EpisodeWorkspace,
    GetAgentDecisionsResult,
    GetQualityReviewResult,
    GetStoryboardResult,
    OperationName,
    WorkflowProgress,
)
from contracts.intervention import ActorType, CommandEnvelope, CommandStatus, Scope


@dataclass(frozen=True, slots=True)
class Caller:
    actor_type: ActorType
    actor_id: str
    scopes: frozenset[Scope]
    #: port だけが解釈する不透明な値（HTTP では認証済み principal）
    principal: object = None
    #: 入口が付けた相関 ID（ADR-0044 §5。ログと command の ``correlation_id`` に使う）
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReceivedCommand:
    command_id: str
    status: CommandStatus
    #: False = 同じ command_id・同じ内容の再送
    created: bool
    change_set_id: str | None = None


class CommandPort(Protocol):
    async def receive(
        self, episode_id: str, envelopes: Sequence[CommandEnvelope], caller: Caller
    ) -> list[ReceivedCommand]: ...

    async def start(self, command_ids: Sequence[str]) -> None: ...


class EpisodeDraftPort(Protocol):
    """agentic Episode の draft（ADR-0043 §6）。実装は infrastructure が構造的に満たす。

    ``create_draft`` は同じ ``command_id`` で内容が違えば ``OperationError(command_id_reused)``。
    日次の枠・TopicPlan を使わず、生成を始めない。
    """

    async def create_draft(self, *, command_id: str, brief: UserBrief) -> DraftRecord: ...

    async def get_brief(self, episode_id: uuid.UUID) -> UserBrief | None:
        """Episode の UserBrief。Episode または UserBrief が無ければ ``None``。"""
        ...


class AgenticWorkflowStarter(Protocol):
    """``AgenticEpisodeWorkflow`` の起動（Temporal の adapter が満たす）。REJECT_DUPLICATE。"""

    async def start(
        self, *, episode_id: uuid.UUID, template_id: str, template_version: str
    ) -> StartedWorkflow: ...

    async def probe_workers(self) -> WorkerCheck:
        """agentic の workflow / agent を拾う worker の確認（ADR-0044 §3）。失敗は ``unknown``。"""
        ...


class EpisodeReadPort(Protocol):
    """読み取り（ADR-0044 §2）。PostgreSQL（と MinIO の成果物）が正本。OpenSearch は読まない。

    Episode が無ければ ``OperationError(episode_not_found)`` を投げる。
    """

    async def pipeline_kind(self, episode_id: uuid.UUID) -> PipelineKind: ...

    async def is_unstarted_agentic_draft(self, episode_id: uuid.UUID) -> bool:
        """brief あり・計画が未 pin（start_agentic_production 前）。resume では始められない。"""
        ...

    async def list_episodes(self, *, limit: int, before: datetime | None) -> EpisodeList: ...

    async def workspace(self, episode_id: uuid.UUID) -> EpisodeWorkspace: ...

    async def storyboard(self, episode_id: uuid.UUID) -> GetStoryboardResult: ...

    async def change_impact(self, episode_id: uuid.UUID, change_set_id: str) -> ChangeImpactPreview:
        """Change set が無い・別 Episode のものなら ``OperationError(change_set_not_found)``。"""
        ...

    async def agent_decisions(self, episode_id: uuid.UUID) -> GetAgentDecisionsResult: ...

    async def quality_review(self, episode_id: uuid.UUID) -> GetQualityReviewResult: ...

    async def progress(self, episode_id: uuid.UUID) -> WorkflowProgress: ...


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """1 操作の観測（ADR-0044 §5）。秘密・入力全文は持たない。"""

    operation: OperationName
    actor_type: ActorType
    #: ``succeeded`` / ``rejected``（OperationError）/ ``failed``（想定外の例外）
    result: str
    duration_ms: int
    episode_id: str | None = None
    correlation_id: str | None = None
    command_id: str | None = None
    workflow_id: str | None = None
    error_code: str | None = None


class OperationObserver(Protocol):
    """操作の観測の出口（HTTP は構造化ログ。将来 OpenTelemetry も同じ口に繋ぐ）。"""

    def record(self, record: OperationRecord) -> None: ...
