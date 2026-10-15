"""操作（ユースケース）の契約（ADR-0043 §1）。**操作の入力・結果・エラー code の唯一の宣言元**。

HTTP・CLI・アシスタント tool・内部 Agent は同じ型を使う（AGENTS.md §8）。
OpenAPI・tool schema はここの model から導出し、手書きしない（INV-52）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.agentic import UserBrief
from contracts.intervention import CommandStatus, SceneOverrideVisual


class OperationErrorCode(StrEnum):
    """操作の失敗 code。既存の文字列（``RejectReason`` など）は同じ綴りで再利用する。"""

    INVALID_PAYLOAD = "invalid_payload"
    UNSUPPORTED_CHANGE = "unsupported_change"
    MISSING_SCOPE = "missing_scope"
    EPISODE_NOT_FOUND = "episode_not_found"
    COMMAND_ID_REUSED = "command_id_reused"
    STALE_CONTROL_REVISION = "stale_control_revision"
    STALE_TARGET_REVISION = "stale_target_revision"
    FORBIDDEN = "forbidden"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_UNVERIFIABLE = "budget_unverifiable"
    # ADR-0043 で新設
    FEATURE_DISABLED = "feature_disabled"
    UNKNOWN_TEMPLATE = "unknown_template"
    UNKNOWN_AGENT_SPEC = "unknown_agent_spec"
    TEMPLATE_NOT_RUNNABLE = "template_not_runnable"


class OperationError(Exception):
    """操作の失敗。port の実装（infrastructure を含む）もこれを投げる。

    例外型は contracts に置く: infrastructure は ``application`` を import できないため
    （INV-6）。``application.errors`` が再 export する。
    """

    def __init__(
        self, code: OperationErrorCode, message: str, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail: dict[str, Any] = dict(detail or {})


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _visual_description_bounds() -> tuple[int, int]:
    """``SceneOverrideVisual.visual_description`` の長さ制約を写す（再掲しない）。"""
    meta = SceneOverrideVisual.model_fields["visual_description"].metadata
    low = next(m.min_length for m in meta if hasattr(m, "min_length"))
    high = next(m.max_length for m in meta if hasattr(m, "max_length"))
    return low, high


_VISUAL_MIN, _VISUAL_MAX = _visual_description_bounds()

MAX_STORYBOARD_EDITS = 12

#: 提案から edit ごとの command_id を決める namespace。変えると再送の冪等性が切れる
STORYBOARD_PROPOSAL_NAMESPACE = uuid.UUID("0b8f6a3e-52c1-5d47-9a1e-3c7f2e6d4b90")


def storyboard_edit_command_id(request_command_id: str, index: int) -> str:
    """同じ要求の同じ edit は同じ command_id（INV-42/50）。"""
    return str(uuid.uuid5(STORYBOARD_PROPOSAL_NAMESPACE, f"{request_command_id}:{index}"))


def storyboard_request_sha256(
    episode_id: uuid.UUID, proposal: BaseModel, expected_control_revision: int | None
) -> str:
    """要求全体（全 edit・件数・順序・基準）の指紋。再送が「同じ要求」かの判定に使う。

    全 edit の command に同じ値を載せるので、edit 数や内容が違う再送は先頭の command で
    ``command_id_reused`` になり、何も書かれない（部分適用なし。INV-42/50）。
    """
    material = {
        "episode_id": str(episode_id),
        "proposal": proposal.model_dump(mode="json"),
        "expected_control_revision": expected_control_revision,
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class StoryboardSceneEdit(_Frozen):
    scene_id: str = Field(min_length=1, max_length=255)
    #: P0 が受け付ける唯一の変更（``scene.override_visual`` に写る）
    visual_description: str | None = Field(
        default=None, min_length=_VISUAL_MIN, max_length=_VISUAL_MAX
    )
    #: 以下は ``storyboard.patch_scenes``（未実装）が要る。指定すると ``unsupported_change``
    framing: str | None = Field(default=None, max_length=200)
    camera_movement: str | None = Field(default=None, max_length=200)
    visual_kind: Literal["generated"] | None = None
    reference_asset_id: str | None = Field(default=None, max_length=128)


class StoryboardPatchProposal(_Frozen):
    schema_version: Literal["1.0"] = "1.0"
    episode_id: uuid.UUID
    base_manifest_id: str | None = None
    base_storyboard_revision_id: str = Field(min_length=1)
    edits: tuple[StoryboardSceneEdit, ...] = Field(min_length=1, max_length=MAX_STORYBOARD_EDITS)
    rationale: str | None = Field(default=None, max_length=4000)


class ProposeStoryboardChangeRequest(_Frozen):
    command_id: str = Field(min_length=8, max_length=64)
    expected_control_revision: int | None = Field(default=None, ge=0)
    proposal: StoryboardPatchProposal


class ProposedSceneCommand(_Frozen):
    scene_id: str
    command_id: str
    status: CommandStatus
    #: False = 同じ内容の再送（既存の command を返した）
    created: bool


class StoryboardProposalResult(_Frozen):
    request_command_id: str
    episode_id: uuid.UUID
    commands: tuple[ProposedSceneCommand, ...]


# --------------------------------------------------------------------------- agentic（ADR-0043 §6）

#: draft の ``episode_id`` を ``command_id`` から決める namespace。変えると再送の冪等性が切れる
EPISODE_DRAFT_NAMESPACE = uuid.UUID("5c1e7d92-3a4b-5f60-8b2d-91e4c6a07f35")


def episode_draft_id(command_id: str) -> uuid.UUID:
    """同じ ``command_id`` は同じ Episode id（draft 作成の冪等性。INV-42 と同じ考え方）。"""
    return uuid.uuid5(EPISODE_DRAFT_NAMESPACE, command_id)


class DraftRecord(_Frozen):
    """port の結果: draft の Episode。infrastructure も作るので contracts に置く（INV-6）。"""

    episode_id: uuid.UUID
    #: False = 同じ command_id・同じ内容の再送
    created: bool


class StartedWorkflow(_Frozen):
    """port の結果: 起動した（または既にあった）workflow。"""

    workflow_id: str
    started: bool


class CreateEpisodeDraftRequest(_Frozen):
    command_id: str = Field(min_length=8, max_length=64)
    brief: UserBrief


class EpisodeDraftResult(_Frozen):
    command_id: str
    episode_id: uuid.UUID
    #: False = 同じ command_id・同じ内容の再送（既存の Episode を返した）
    created: bool


class StartAgenticProductionRequest(_Frozen):
    episode_id: uuid.UUID


class StartAgenticProductionResult(_Frozen):
    episode_id: uuid.UUID
    workflow_id: str
    #: False = 同じ id の workflow が既にある（REJECT_DUPLICATE。二重に始めない）
    started: bool
