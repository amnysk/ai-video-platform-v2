"""P2-A の操作カタログと入出力契約（ADR-0044）。**操作の名前・権限・効果・入出力型の唯一の宣言元**。

Web UI（HTTP）・将来の MCP・CLI・内部 Agent は同じ ``OPERATIONS`` を参照する。HTTP route の
request/response model と method/path はここから取り、手書きで再掲しない（INV-52）。

- 書き込みは既存の介入 command（``CommandEnvelope``）を通す。ここに適用ロジックは無い。
- 変更案の作成（``propose_storyboard_change``）と適用（``apply_approved_change``）は別操作。
- 費用は USD の10進文字列。**未見積りは ``None``**（0 として表示しない）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.agentic import QualityReview
from contracts.agentic_pipeline import PipelineKind
from contracts.application import (
    CreateEpisodeDraftRequest,
    EpisodeDraftResult,
    ProposeStoryboardChangeRequest,
    StartAgenticProductionResult,
    StoryboardProposalResult,
    WorkerCheck,
)
from contracts.artifacts import StoryboardScene
from contracts.assistant_tools import ToolEffect
from contracts.intervention import CommandStatus, Scope
from contracts.states import EpisodeStatus


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


#: P2-A で編集できる絵コンテの属性（``StoryboardSceneEdit.visual_description`` だけ）
EDITABLE_STORYBOARD_FIELDS: tuple[str, ...] = ("visual_description",)
#: 指定すると ``unsupported_change``（``storyboard.patch_scenes`` 未実装 / ADR-0043）
UNSUPPORTED_STORYBOARD_FIELDS: tuple[str, ...] = (
    "framing",
    "camera_movement",
    "visual_kind",
    "reference_asset_id",
)


# --------------------------------------------------------------------------- 読み取り


class EpisodeSummary(_Frozen):
    episode_id: uuid.UUID
    #: ``EpisodeStatus`` の値（application domain state。Temporal の実行状態ではない / INV-8）
    status: str
    topic: str | None
    pipeline_kind: PipelineKind
    created_at: datetime
    updated_at: datetime


class ListEpisodesRequest(_Frozen):
    limit: int = Field(default=50, ge=1, le=200)
    #: keyset cursor。``created_at`` がこれより前の Episode（新しい順）
    before: datetime | None = None
    #: 複合 cursor の後半（ADR-0045 §5）。``before`` と一緒に渡すと ``(created_at, id)`` で比べる。
    #: 無ければ従来どおり ``created_at`` だけ（後方互換）。同時刻の Episode を落とさない
    before_id: uuid.UUID | None = None
    #: 状態での絞り込み（``EpisodeStatus``）
    status: EpisodeStatus | None = None
    #: 題材（topic）の部分一致（大文字小文字を区別しない。``%`` ``_`` は文字として扱う）
    q: str | None = Field(default=None, min_length=1, max_length=200)


class EpisodeList(_Frozen):
    episodes: tuple[EpisodeSummary, ...]
    #: 次のページの ``before``。無ければ最後のページ
    next_before: datetime | None
    #: 次のページの ``before_id``（``next_before`` と対）。最後のページなら None
    next_before_id: uuid.UUID | None = None


class ChangeSetSummary(_Frozen):
    change_set_id: str
    kind: str
    status: str
    base_revision_id: str
    #: USD の10進文字列。未見積りは None
    estimated_cost_usd: str | None
    cost_estimated: bool
    proposed_by_command_id: str
    actor_type: str
    created_at: str


class EpisodeWorkspace(_Frozen):
    episode: EpisodeSummary
    #: 本流の workflow id（決定論的。agentic なら ``agentic-episode-{id}``）
    workflow_id: str
    control_revision: int
    pending_change_sets: tuple[ChangeSetSummary, ...]
    #: 既存の Episode 読み取りモデル（``/v1/intervention/episodes/{id}`` と同じ形）
    read_model: dict[str, Any]


class StoryboardView(_Frozen):
    episode_id: uuid.UUID
    #: 提案の ``base_storyboard_revision_id`` に渡す値
    revision_id: str
    manifest_id: str | None
    schema_version: str
    sha256: str
    scenes: tuple[StoryboardScene, ...]
    editable_fields: tuple[str, ...] = EDITABLE_STORYBOARD_FIELDS
    unsupported_fields: tuple[str, ...] = UNSUPPORTED_STORYBOARD_FIELDS


class GetStoryboardResult(_Frozen):
    episode_id: uuid.UUID
    #: 現行の storyboard が無ければ None
    storyboard: StoryboardView | None


class ChangeImpactPreview(_Frozen):
    episode_id: uuid.UUID
    change_set_id: str
    kind: str
    status: str
    base_revision_id: str
    base_manifest_id: str | None
    proposal: dict[str, Any]
    #: 影響範囲（stale・再生成・再利用）。未計算なら None
    impact: dict[str, Any] | None
    estimated_cost_usd: str | None
    cost_estimated: bool
    #: 適用の ``expected_control_revision`` に渡す値
    control_revision: int
    #: 今この変更を適用できない理由（機械可読 code。空なら適用要求を出せる）
    blockers: tuple[str, ...]
    #: 適用・却下を決めた command（未決なら None）。同じ command の再送を見分ける
    decided_by_command_id: str | None = None


class GetAgentDecisionsResult(_Frozen):
    episode_id: uuid.UUID
    #: 判断記録（読み取りモデルの ``DecisionView`` の形）
    decisions: tuple[dict[str, Any], ...]
    #: 読み取りモデルの agent run（``AgentRunView`` の形）
    agent_runs: tuple[dict[str, Any], ...]


class GetQualityReviewResult(_Frozen):
    episode_id: uuid.UUID
    status: Literal["reviewed", "not_reviewed"]
    review: QualityReview | None
    artifact_id: str | None
    #: ``review_finding`` 注記（``AnnotationView`` の形）
    findings: tuple[dict[str, Any], ...]


class JobProgress(_Frozen):
    job_id: str
    type: str
    status: str
    attempts: int
    max_attempts: int
    scene_id: str | None
    updated_at: datetime


class FailureInfo(_Frozen):
    #: 機械可読な失敗分類（job の failure class・agent run の outcome 等）
    code: str
    message: str | None
    source: Literal["job", "agent_run", "command", "episode"]
    source_id: str | None
    at: datetime | str | None


class WorkflowProgress(_Frozen):
    """進行状況（PostgreSQL が正本。Temporal へ問い合わせない）。"""

    episode_id: uuid.UUID
    episode_status: str
    pipeline_kind: PipelineKind
    workflow_id: str
    jobs: tuple[JobProgress, ...]
    #: agentic の stage 実行（``AgentRunView`` の形）
    agent_runs: tuple[dict[str, Any], ...]
    #: 直近の失敗。無ければ None
    failure: FailureInfo | None


# --------------------------------------------------------------------------- 書き込み（command）


class RequestHoldRequest(_Frozen):
    command_id: str = Field(min_length=8, max_length=64)
    expected_control_revision: int | None = Field(default=None, ge=0)
    reason: str | None = Field(default=None, max_length=1000)


class ResumeProductionRequest(_Frozen):
    command_id: str = Field(min_length=8, max_length=64)
    expected_control_revision: int | None = Field(default=None, ge=0)
    #: legacy だけが使う。agentic は固定計画を先頭から歩く（完了済み stage は再利用）
    start_stage: str | None = None


class ApplyApprovedChangeRequest(_Frozen):
    command_id: str = Field(min_length=8, max_length=64)
    expected_control_revision: int | None = Field(default=None, ge=0)
    #: 人が承認した見積りの上限（USD）。**必須**: 見積りを見てから適用する
    approved_max_cost_usd: float = Field(ge=0)


class CommandAccepted(_Frozen):
    """command を受け付けた（``received``）。完了は ``GET /commands/{id}`` で別に確かめる。"""

    episode_id: uuid.UUID
    command_id: str
    status: CommandStatus
    #: False = 同じ command_id・同じ内容の再送
    created: bool


class ResumeAccepted(CommandAccepted):
    pipeline_kind: PipelineKind
    #: agentic の worker の確認結果（legacy は ``unknown``）
    worker_check: WorkerCheck = WorkerCheck.UNKNOWN


# --------------------------------------------------------------------------- カタログ


class OperationName(StrEnum):
    CREATE_EPISODE_DRAFT = "create_episode_draft"
    LIST_EPISODES = "list_episodes"
    GET_EPISODE_WORKSPACE = "get_episode_workspace"
    START_AGENTIC_PRODUCTION = "start_agentic_production"
    REQUEST_HOLD = "request_hold"
    RESUME_PRODUCTION = "resume_production"
    GET_STORYBOARD = "get_storyboard"
    PROPOSE_STORYBOARD_CHANGE = "propose_storyboard_change"
    PREVIEW_CHANGE_IMPACT = "preview_change_impact"
    APPLY_APPROVED_CHANGE = "apply_approved_change"
    GET_AGENT_DECISIONS = "get_agent_decisions"
    GET_QUALITY_REVIEW = "get_quality_review"
    GET_WORKFLOW_PROGRESS = "get_workflow_progress"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: OperationName
    effect: ToolEffect
    required_scope: Scope
    #: 人（``ActorType.HUMAN``）だけが呼べる（有料の生成を始める操作）
    human_only: bool
    #: body の model（無ければ None。path・query だけの操作）
    request_model: type[BaseModel] | None
    result_model: type[BaseModel]
    http_method: Literal["GET", "POST"]
    #: ``/v1/intervention`` からの相対 path
    http_path: str
    #: GET の query parameter の model（無ければ None）。``request_model``（body）とは別
    query_model: type[BaseModel] | None = None


_EP = "/episodes/{episode_id}"

OPERATIONS: tuple[OperationSpec, ...] = (
    OperationSpec(
        OperationName.CREATE_EPISODE_DRAFT,
        ToolEffect.PROPOSE,
        Scope.PROPOSE,
        False,
        CreateEpisodeDraftRequest,
        EpisodeDraftResult,
        "POST",
        "/episodes/drafts",
    ),
    OperationSpec(
        OperationName.LIST_EPISODES,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        EpisodeList,
        "GET",
        "/episodes",
        ListEpisodesRequest,
    ),
    OperationSpec(
        OperationName.GET_EPISODE_WORKSPACE,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        EpisodeWorkspace,
        "GET",
        f"{_EP}/workspace",
    ),
    OperationSpec(
        OperationName.START_AGENTIC_PRODUCTION,
        ToolEffect.CONTROL,
        Scope.RESUME,
        True,
        None,
        StartAgenticProductionResult,
        "POST",
        f"{_EP}/production",
    ),
    OperationSpec(
        OperationName.REQUEST_HOLD,
        ToolEffect.CONTROL,
        Scope.HOLD,
        False,
        RequestHoldRequest,
        CommandAccepted,
        "POST",
        f"{_EP}/hold",
    ),
    OperationSpec(
        OperationName.RESUME_PRODUCTION,
        ToolEffect.CONTROL,
        Scope.RESUME,
        False,
        ResumeProductionRequest,
        ResumeAccepted,
        "POST",
        f"{_EP}/resume",
    ),
    OperationSpec(
        OperationName.GET_STORYBOARD,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        GetStoryboardResult,
        "GET",
        f"{_EP}/storyboard",
    ),
    OperationSpec(
        OperationName.PROPOSE_STORYBOARD_CHANGE,
        ToolEffect.PROPOSE,
        Scope.PROPOSE,
        False,
        ProposeStoryboardChangeRequest,
        StoryboardProposalResult,
        "POST",
        f"{_EP}/storyboard/proposals",
    ),
    OperationSpec(
        OperationName.PREVIEW_CHANGE_IMPACT,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        ChangeImpactPreview,
        "GET",
        f"{_EP}/change-sets/{{change_set_id}}/impact",
    ),
    OperationSpec(
        OperationName.APPLY_APPROVED_CHANGE,
        ToolEffect.APPLY,
        Scope.APPLY,
        False,
        ApplyApprovedChangeRequest,
        CommandAccepted,
        "POST",
        f"{_EP}/change-sets/{{change_set_id}}/apply",
    ),
    OperationSpec(
        OperationName.GET_AGENT_DECISIONS,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        GetAgentDecisionsResult,
        "GET",
        f"{_EP}/agent-decisions",
    ),
    OperationSpec(
        OperationName.GET_QUALITY_REVIEW,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        GetQualityReviewResult,
        "GET",
        f"{_EP}/quality-review",
    ),
    OperationSpec(
        OperationName.GET_WORKFLOW_PROGRESS,
        ToolEffect.READ,
        Scope.READ,
        False,
        None,
        WorkflowProgress,
        "GET",
        f"{_EP}/progress",
    ),
)

OPERATIONS_BY_NAME: dict[OperationName, OperationSpec] = {o.name: o for o in OPERATIONS}
