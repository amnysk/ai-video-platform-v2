"""判断記録と介入の語彙・payload（JP-MAS-001 v2.0 / ADR-0041）。

ここが**唯一の宣言元**（AGENTS.md §8）。DB の CHECK は migration 0016 が literal で凍結し、
``tests/contract/test_intervention_contracts.py`` が両者の一致を検査する。
設計の論理エンティティと物理表の対応は ``docs/domain/intervention.md``。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

INTERVENTION_SCHEMA_VERSION = "1.0"

# --------------------------------------------------------------------------- 語彙


class ActorType(StrEnum):
    """変更・判断の主体。AI の提案を人の直接編集として記録しない（設計 §6）。"""

    HUMAN = "human"
    AI_ASSISTANT = "ai_assistant"  # ユーザーと対話する AI（外部。API の principal として来る）
    AGENT = "agent"  # 制作の実行役（producer / writer_director / reviewer）
    SYSTEM = "system"


class InterventionMode(StrEnum):
    LEGACY = "legacy"
    AUTO_INTERVENABLE = "auto_intervenable"
    REVIEW_POINTS = "review_points"
    CO_PRODUCTION = "co_production"


#: ``episode_controls`` 行が無い Episode の読み方（古い Episode・日次実行）
DEFAULT_MODE = InterventionMode.LEGACY


class ControlState(StrEnum):
    RUNNING = "running"
    HOLD_REQUESTED = "hold_requested"
    HELD = "held"
    WAITING_REVIEW = "waiting_review"
    CANCEL_REQUESTED = "cancel_requested"


DEFAULT_CONTROL_STATE = ControlState.RUNNING


class ReviewPoint(StrEnum):
    """待機地点。pipeline の工程（``contracts.pipeline.PipelineStage``）の**開始前**に対応する。"""

    BEFORE_STORYBOARD = "before_storyboard"  # 台本確定前（script の後）
    BEFORE_PRODUCTION = "before_production"  # 有料素材生成前
    BEFORE_RENDER = "before_render"  # 素材取得後
    BEFORE_UPLOAD = "before_upload"  # 完成品確定前


#: co_production の既定の待機地点
CO_PRODUCTION_REVIEW_POINTS: tuple[ReviewPoint, ...] = (
    ReviewPoint.BEFORE_STORYBOARD,
    ReviewPoint.BEFORE_PRODUCTION,
    ReviewPoint.BEFORE_UPLOAD,
)


class WaitTimeoutPolicy(StrEnum):
    """待機期限の扱い。自動承認は語彙に**無い**（設計 §4）。"""

    KEEP_WAITING = "keep_waiting"


#: mode・待機地点・期限方針の組の版（``episode_controls.policy_version``）
INTERVENTION_POLICY_VERSION = "jpmas-policy-1"


class CommandType(StrEnum):
    """介入 command の型。予算上限・Schedule・公開設定を変える型は**作らない**（INV-47）。"""

    COMMENT = "comment"
    REQUEST_HOLD = "request_hold"
    PROPOSE_CHANGE = "propose_change"
    APPLY_CHANGE = "apply_change"
    REJECT_CHANGE = "reject_change"
    RESUME = "resume"
    RELEASE_CHECKPOINT = "release_checkpoint"
    SET_USER_CONSTRAINT = "set_user_constraint"
    SET_MODE = "set_mode"
    REQUEST_CANCEL = "request_cancel"
    RESOLVE_ANNOTATION = "resolve_annotation"


class CommandStatus(StrEnum):
    RECEIVED = "received"
    VALIDATED = "validated"
    APPLYING = "applying"
    APPLIED = "applied"
    CONFLICT = "conflict"
    REJECTED = "rejected"
    FAILED = "failed"


TERMINAL_COMMAND_STATUSES: frozenset[CommandStatus] = frozenset(
    {CommandStatus.APPLIED, CommandStatus.CONFLICT, CommandStatus.REJECTED, CommandStatus.FAILED}
)


class Scope(StrEnum):
    """principal の権限。command 型ごとの必要 scope は ``REQUIRED_SCOPE``。"""

    READ = "read"
    COMMENT = "comment"
    PROPOSE = "propose"
    HOLD = "hold"
    APPLY = "apply"
    RESUME = "resume"
    SET_MODE = "set_mode"


REQUIRED_SCOPE: dict[CommandType, Scope] = {
    CommandType.COMMENT: Scope.COMMENT,
    CommandType.SET_USER_CONSTRAINT: Scope.COMMENT,
    CommandType.RESOLVE_ANNOTATION: Scope.COMMENT,
    CommandType.REQUEST_HOLD: Scope.HOLD,
    CommandType.REQUEST_CANCEL: Scope.HOLD,
    CommandType.PROPOSE_CHANGE: Scope.PROPOSE,
    CommandType.APPLY_CHANGE: Scope.APPLY,
    CommandType.REJECT_CHANGE: Scope.APPLY,
    CommandType.RELEASE_CHECKPOINT: Scope.APPLY,
    CommandType.RESUME: Scope.RESUME,
    CommandType.SET_MODE: Scope.SET_MODE,
}

#: AI（``ai_assistant`` / ``agent``）には付与できない scope（設定読み込み時に拒否）
HUMAN_ONLY_SCOPES: frozenset[Scope] = frozenset({Scope.SET_MODE})


class RejectReason(StrEnum):
    """REJECTED / CONFLICT / FAILED の機械可読な理由。"""

    UNSUPPORTED_COMMAND = "unsupported_command"
    UNSUPPORTED_CHANGE = "unsupported_change"
    FORBIDDEN = "forbidden"
    STALE_CONTROL_REVISION = "stale_control_revision"
    STALE_TARGET_REVISION = "stale_target_revision"
    INVALID_STATE = "invalid_state"
    USER_CONSTRAINT_VIOLATION = "user_constraint_violation"
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_UNVERIFIABLE = "budget_unverifiable"
    INVALID_PAYLOAD = "invalid_payload"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"


class ForbiddenDetail(StrEnum):
    """``reject_reason=forbidden`` の原因を区別する機械可読な ``result.detail``。"""

    SCOPE_MISMATCH = "scope_mismatch"
    HUMAN_ONLY = "human_only"
    #: AI の apply_change は 0 円・制約に触れない変更だけ（有料の変更は人）
    AI_PAID_APPLY = "ai_paid_apply"
    #: AI の resume / release_checkpoint は、有料の生成（画像・動画）を伴わない実行だけ
    AI_RESUME_REQUIRES_PAID_GENERATION = "ai_resume_requires_paid_generation"


class AnnotationKind(StrEnum):
    COMMENT = "comment"
    REVIEW_FINDING = "review_finding"
    USER_CONSTRAINT = "user_constraint"
    APPROVAL = "approval"
    RESOLUTION = "resolution"  # 既存注記の解決・撤回（元の行は消さない）


class ChangeSetStatus(StrEnum):
    PROPOSED = "proposed"
    APPLIED = "applied"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"
    NOT_SELECTED = "not_selected"  # 別案（Variant）が採用された


class ChangeKind(StrEnum):
    SCRIPT_EDIT_SCENES = "script.edit_scenes"
    SCENE_OVERRIDE_VISUAL = "scene.override_visual"


class RevisionLinkKind(StrEnum):
    PARENT = "parent"  # 同じ論理成果物の前の revision
    DERIVED_FROM = "derived_from"  # 下流が上流を入力にした
    REBOUND_TO = "rebound_to"  # storyboard を構成の同じ新台本へ結び直した（ADR-0041 §6）
    EVIDENCE = "evidence"


class AgentRole(StrEnum):
    """実行役の語彙の唯一の宣言元（設計 §3）。``contracts/roles.py`` の RoleSpec と1対1。

    初期の3役（producer / writer_director / reviewer）に、ADR-0043 が自律改善型制作の4役
    （strategy / creative_director / quality_reviewer / learning）を足した。版付きの定義は
    ``contracts/agents.py``（Agent Registry）が持つ。
    """

    PRODUCER = "producer"
    WRITER_DIRECTOR = "writer_director"
    REVIEWER = "reviewer"
    STRATEGY = "strategy"
    CREATIVE_DIRECTOR = "creative_director"
    QUALITY_REVIEWER = "quality_reviewer"
    LEARNING = "learning"


class AgentRunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED_OUTPUT = "rejected_output"


class RationaleKind(StrEnum):
    REPORTED = "reported"  # モデルが自己申告した理由
    POST_HOC_SUMMARY = "post_hoc_summary"  # 採用後に生成した説明
    HUMAN = "human"
    RULE = "rule"  # 決定論の規則による選択
    NONE = "none"  # 理由の記録が無い（補完しない）


class AssessmentKind(StrEnum):
    SELF_ASSESSED = "self_assessed"
    MEASURED = "measured"
    RULE = "rule"


class EvidenceCheck(StrEnum):
    VERIFIED_IN_INPUT = "verified_in_input"
    UNVERIFIED_REF = "unverified_ref"  # 入力 snapshot に無い資料を根拠に挙げた
    NOT_CHECKED = "not_checked"


class InterventionEvent(StrEnum):
    """``episode_events.event_name``。ログの ``EventName`` と同じ文字列。

    設計 §15 の名前を既存ログ語彙の dotted 形式に写す。
    """

    DECISION_RECORDED = "decision.recorded"
    AGENT_RUN_RECORDED = "agent.run.recorded"
    COMMAND_RECEIVED = "intervention.command.received"
    COMMAND_FINISHED = "intervention.command.finished"
    COMMENT_ADDED = "intervention.comment.added"
    HOLD_REQUESTED = "intervention.hold.requested"
    HOLD_EFFECTIVE = "intervention.hold.effective"
    CHANGE_PROPOSED = "intervention.change.proposed"
    CHANGE_APPLIED = "intervention.change.applied"
    CHANGE_CONFLICTED = "intervention.change.conflicted"
    VARIANT_SELECTED = "intervention.variant.selected"
    REVIEW_INVALIDATED = "review.invalidated"
    REVIEW_RESOLVED = "review.resolved"
    CHECKPOINT_WAITING = "checkpoint.waiting"
    CHECKPOINT_RELEASED = "checkpoint.released"
    RESUME_REQUESTED = "intervention.resume.requested"
    RESUME_COMPLETED = "intervention.resume.completed"
    MODE_CHANGED = "intervention.mode.changed"


# --------------------------------------------------------------------------- 予算（設計 §2）

#: 1本・1か月の上限（円）。**AI・介入 API から変更する経路を作らない**（INV-47）。
#: 実効値は Settings（``EPISODE_BUDGET_JPY`` / ``MONTHLY_BUDGET_JPY``）で、所有者だけが変える。
DEFAULT_EPISODE_BUDGET_JPY = 1000
DEFAULT_MONTHLY_BUDGET_JPY = 30000

# --------------------------------------------------------------------------- Temporal


class PipelineStartOutcome(StrEnum):
    """command の result の ``pipeline.outcome``。follow-up が必ず1つ記録する。"""

    STARTED = "started"
    ALREADY_RUNNING = "already_running"
    #: start が失敗した（再配送が再試行する）
    START_FAILED = "start_failed"
    #: 適用の後に状況が変わり、起動してはならない（制御版・状態・Episode の終端）。起動しない
    NOT_NEEDED = "not_needed"


INTERVENTION_TASK_QUEUE = "intervention"
INTERVENTION_COMMAND_WORKFLOW = ("InterventionCommandWorkflow", INTERVENTION_TASK_QUEUE)
INTERVENTION_REDELIVERY_WORKFLOW = ("InterventionRedeliveryWorkflow", INTERVENTION_TASK_QUEUE)
INTERVENTION_REDELIVERY_SCHEDULE_ID = "avp-intervention-redelivery"
APPLY_INTERVENTION_COMMAND_ACTIVITY = "apply_intervention_command"
RECORD_COMMAND_FOLLOWUP_ACTIVITY = "record_intervention_command_followup"
LIST_UNDELIVERED_COMMANDS_ACTIVITY = "list_undelivered_intervention_commands"
MARK_COMMAND_DISPATCHED_ACTIVITY = "mark_intervention_command_dispatched"
CHECK_PIPELINE_START_ACTIVITY = "check_intervention_pipeline_start"
PIPELINE_CONTROL_GATE_ACTIVITY = "pipeline_control_gate"
CONTROL_GATE_PATCH_ID = "jpmas-control-gate-v1"
#: RECEIVED のまま、この秒数を超えて終端にならない command を再配送の対象にする
REDELIVERY_STALE_AFTER_SECONDS = 120


def intervention_command_workflow_id(command_id: str) -> str:
    return f"intervention-{command_id}"


# --------------------------------------------------------------------------- payload


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimeRange(_Frozen):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)


class Anchor(_Frozen):
    """コメントの対象（設計 §8）。版が変わっても元の対象を保持し、自動で移動しない。"""

    revision_id: str | None = None
    manifest_id: str | None = None
    segment_id: str | None = None  # 台本のシーン id（ScriptScene.id）
    scene_id: str | None = None  # storyboard のシーン id
    asset_id: str | None = None
    time_range: TimeRange | None = None


class CommentPayload(_Frozen):
    anchor: Anchor
    body: str = Field(min_length=1, max_length=4000)
    kind: Literal[AnnotationKind.COMMENT, AnnotationKind.REVIEW_FINDING] = AnnotationKind.COMMENT


class AnnotationResolution(StrEnum):
    """注記の解決の宣言（``resolve_annotation``）。"""

    RESOLVED = "resolved"
    WONT_FIX = "wont_fix"


class ResolvePayload(_Frozen):
    """comment / review_finding 注記の解決。元の注記は変えず resolution 注記を追記する。"""

    annotation_id: str = Field(min_length=1, max_length=64)
    resolution: AnnotationResolution
    note: str | None = Field(default=None, min_length=1, max_length=1000)


class UserConstraintPayload(_Frozen):
    """「この文章は保持」「このシーンは変更しない」。検証できる対象 ID を必ず持つ（設計 §9）。"""

    rule: Literal["keep_segment_text", "keep_scene_visual"]
    target_ids: tuple[str, ...] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class SceneTextEdit(_Frozen):
    segment_id: str
    narration: str | None = Field(default=None, min_length=1, max_length=220)
    visual: str | None = Field(default=None, min_length=1, max_length=400)


class ScriptEditScenes(_Frozen):
    kind: Literal[ChangeKind.SCRIPT_EDIT_SCENES] = ChangeKind.SCRIPT_EDIT_SCENES
    base_revision_id: str
    title: str | None = Field(default=None, min_length=1, max_length=100)
    hook: str | None = Field(default=None, min_length=1, max_length=80)
    edits: tuple[SceneTextEdit, ...] = ()


class SceneOverrideVisual(_Frozen):
    kind: Literal[ChangeKind.SCENE_OVERRIDE_VISUAL] = ChangeKind.SCENE_OVERRIDE_VISUAL
    base_revision_id: str  # storyboard の revision
    scene_id: str
    visual_description: str = Field(min_length=1, max_length=600)


Change = Annotated[ScriptEditScenes | SceneOverrideVisual, Field(discriminator="kind")]


class ProposeChangePayload(_Frozen):
    """ChangeSet の提案。ReviewPacket から取り込む変更案もこの形（``ChangeProposalFile``）。"""

    change: Change
    rationale: str | None = Field(default=None, max_length=4000)
    base_manifest_id: str | None = None
    variant_group: str | None = Field(default=None, max_length=64)


class ApplyChangePayload(_Frozen):
    change_set_id: str
    #: 承認された見積りの上限（USD）。見積りがこれを超えたら適用しない
    max_estimated_cost_usd: float | None = Field(default=None, ge=0)


class RejectChangePayload(_Frozen):
    change_set_id: str
    reason: str | None = Field(default=None, max_length=1000)


class RequestHoldPayload(_Frozen):
    reason: str | None = Field(default=None, max_length=1000)


class ResumePayload(_Frozen):
    #: 省略時は stale の解析から必要な工程を決める
    start_stage: str | None = None


class ReleaseCheckpointPayload(_Frozen):
    point: ReviewPoint
    manifest_id: str  # 承認する manifest（現在の採用 manifest と一致しなければ CONFLICT）
    note: str | None = Field(default=None, max_length=1000)


class SetModePayload(_Frozen):
    mode: InterventionMode
    review_points: tuple[ReviewPoint, ...] | None = None


class CommandEnvelope(_Frozen):
    """API・ReviewPacket 取り込み・将来の adapter が共通に使う command の形。"""

    command_id: str = Field(min_length=8, max_length=64)
    type: CommandType
    expected_control_revision: int | None = Field(default=None, ge=0)
    expected_target_revision: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, max_length=64)


PAYLOAD_MODELS: dict[CommandType, type[BaseModel]] = {
    CommandType.COMMENT: CommentPayload,
    CommandType.SET_USER_CONSTRAINT: UserConstraintPayload,
    CommandType.RESOLVE_ANNOTATION: ResolvePayload,
    CommandType.REQUEST_HOLD: RequestHoldPayload,
    CommandType.REQUEST_CANCEL: RequestHoldPayload,
    CommandType.PROPOSE_CHANGE: ProposeChangePayload,
    CommandType.APPLY_CHANGE: ApplyChangePayload,
    CommandType.REJECT_CHANGE: RejectChangePayload,
    CommandType.RESUME: ResumePayload,
    CommandType.RELEASE_CHECKPOINT: ReleaseCheckpointPayload,
    CommandType.SET_MODE: SetModePayload,
}


def payload_sha256(envelope: CommandEnvelope) -> str:
    """同じ command_id の再送が「同じ入力」かを判定する指紋（INV-42）。"""
    material = {
        "type": envelope.type.value,
        "expected_control_revision": envelope.expected_control_revision,
        "expected_target_revision": envelope.expected_target_revision,
        "payload": envelope.payload,
    }
    raw = json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- 判断記録


class DecisionCandidate(_Frozen):
    id: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    benefits: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()
    #: 数値評価。``assessment_kind`` を必ず伴う（自己評価を実測として扱わない）
    scores: dict[str, float] | None = None
    assessment_kind: AssessmentKind | None = None
    #: モデルの自己評価（常に ``self_assessed``）。規則の点数（``scores``）と同じ候補に両方が
    #: あるとき、種別を混ぜないために分けて持つ（Topic の audience_fit / visual_fit）
    self_assessed_scores: dict[str, float] | None = None
    rejected_reason: str | None = Field(default=None, max_length=2000)


class DecisionRecordPayload(_Frozen):
    """``decision_records.payload``（設計 §7）。候補・スコアを後から補完しない。"""

    schema_version: Literal["1.0"] = "1.0"
    decision_type: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=2000)
    candidates: tuple[DecisionCandidate, ...] = Field(min_length=1)
    selected_candidate: str | None = None
    rationale_kind: RationaleKind
    rationale: str | None = Field(default=None, max_length=4000)
    evidence_refs: tuple[str, ...] = ()
    evidence_check: EvidenceCheck = EvidenceCheck.NOT_CHECKED
    input_snapshot_ref: str | None = None
    tool_invocation_refs: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    uncertainties: tuple[str, ...] = ()
    target_revisions: tuple[str, ...] = ()
    result_revision: str | None = None
    #: 人が上書きした場合の指示（command_id）と前後の差分の参照
    override_command_id: str | None = None
    override_diff: dict[str, Any] | None = None
