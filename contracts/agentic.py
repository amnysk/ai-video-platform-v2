"""自律改善型制作で保存する成果物の契約（ADR-0043 §4、JP-MAS-002 v1.1 §3 / §8）。

内容と版は既存の ``artifact_metadata`` + MinIO に保存する（Agent 専用の表は作らない）。
全ての model は ``schema_version: "1.0"`` を持ち、``extra="forbid"``・frozen。``type`` は
``contracts.states.ArtifactType`` の値で、既存の成果物（storyboard 等）と同じく payload に持つ。

**この契約に無いもの**: 公開設定・予算上限・Schedule（INV-47、ADR-0043）。UserBrief は人が決めてよい
「何を作るか」だけを持ち、投稿の可視性（private 固定、AGENTS.md §9）や費用は持てない。

LessonStatus の遷移表（``LESSON_TRANSITIONS``）と WorkflowPlan の hash 計算
（``compute_plan_hash``）もここに置く（定義は1箇所、AGENTS.md §8）。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from contracts.intervention import AgentRole, TimeRange
from contracts.states import ArtifactType
from contracts.topic import TOPIC_MAX_CHARS
from contracts.topic_planning import (
    DEFAULT_CONTENT_PROFILE_ID,
    DEFAULT_STRATEGY_PROFILE_ID,
    SLUG_PATTERN,
    TopicAngle,
)

SCHEMA_VERSION = "1.0"

#: TopicPlan / TopicCandidate（contracts/topic_planning.py）と揃えた長さ。CreativeBrief は台本の
#: ScriptBrief（subject / angle / era / hook）へそのまま渡せる。
SUBJECT_MAX_CHARS = 80
ERA_MAX_CHARS = 40
HOOK_MIN_CHARS = 5
HOOK_MAX_CHARS = 300

DEFAULT_TARGET_AUDIENCE = "US viewers in their 20s"
DEFAULT_FORMAT_PROFILE = "shorts_en"

_Short = Annotated[str, StringConstraints(min_length=1, max_length=200)]
_Text = Annotated[str, StringConstraints(min_length=1, max_length=500)]
_Note = Annotated[str, StringConstraints(min_length=1, max_length=300)]
_Id = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- UserBrief


class UserBrief(_Model):
    """人の制作依頼。**予算・公開設定・Schedule の項目は持たない**（ADR-0043）。"""

    type: Literal[ArtifactType.USER_BRIEF] = ArtifactType.USER_BRIEF
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    theme: str = Field(min_length=1, max_length=200)
    target_language: Literal["en-US"] = "en-US"
    target_audience: str = Field(default=DEFAULT_TARGET_AUDIENCE, min_length=1, max_length=200)
    format_profile: str = Field(default=DEFAULT_FORMAT_PROFILE, min_length=1, max_length=64)
    desired_duration_seconds: int | None = Field(default=None, ge=15, le=600)
    tone: str | None = Field(default=None, max_length=200)
    must_keep: tuple[_Short, ...] = Field(default=(), max_length=10)
    notes: str | None = Field(default=None, max_length=2000)
    template_id: str = Field(min_length=1, max_length=64)
    template_version: str = Field(min_length=1, max_length=16)
    strategy_profile_id: str = Field(
        default=DEFAULT_STRATEGY_PROFILE_ID, min_length=1, max_length=64
    )
    content_profile_id: str = Field(default=DEFAULT_CONTENT_PROFILE_ID, min_length=1, max_length=64)


# --------------------------------------------------------------------------- Strategy


class LessonStatus(StrEnum):
    """Lesson の検証状態（設計 §4）。``validated`` だけが次回 Strategy の高信頼な参考になる。"""

    PROPOSED = "proposed"
    TESTING = "testing"
    VALIDATED = "validated"
    REJECTED = "rejected"
    RETIRED = "retired"


#: 許される遷移。表に無い辺は存在しない（rejected・retired は終端）。
LESSON_TRANSITIONS: dict[LessonStatus, frozenset[LessonStatus]] = {
    LessonStatus.PROPOSED: frozenset(
        {LessonStatus.TESTING, LessonStatus.REJECTED, LessonStatus.RETIRED}
    ),
    LessonStatus.TESTING: frozenset(
        {LessonStatus.VALIDATED, LessonStatus.REJECTED, LessonStatus.RETIRED}
    ),
    LessonStatus.VALIDATED: frozenset({LessonStatus.RETIRED}),
    LessonStatus.REJECTED: frozenset(),
    LessonStatus.RETIRED: frozenset(),
}


class LessonUse(_Model):
    """Strategy が Lesson を入力にした記録。validated 以外を「検証済みの参考」と呼ばせない。"""

    lesson_revision_id: _Id
    lesson_status: LessonStatus
    used_as: Literal["validated_reference", "hypothesis"]

    @model_validator(mode="after")
    def _only_validated_is_a_reference(self) -> LessonUse:
        if (
            self.used_as == "validated_reference"
            and self.lesson_status is not LessonStatus.VALIDATED
        ):
            raise ValueError("only a validated lesson can be used as a validated_reference")
        return self


class StrategyCandidate(_Model):
    id: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=5, max_length=TOPIC_MAX_CHARS)
    subject: str = Field(pattern=SLUG_PATTERN, max_length=SUBJECT_MAX_CHARS)
    angle: TopicAngle
    era: str = Field(pattern=SLUG_PATTERN, max_length=ERA_MAX_CHARS)
    hook: str = Field(min_length=HOOK_MIN_CHARS, max_length=HOOK_MAX_CHARS)
    rationale: _Text
    risks: tuple[_Note, ...] = Field(default=(), max_length=8)
    status: Literal["proposed", "rejected"]
    rejected_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reason_iff_rejected(self) -> StrategyCandidate:
        if self.status == "rejected" and self.rejected_reason is None:
            raise ValueError("a rejected candidate needs rejected_reason")
        if self.status == "proposed" and self.rejected_reason is not None:
            raise ValueError("rejected_reason is only for rejected candidates")
        return self


class StrategyBrief(_Model):
    type: Literal[ArtifactType.STRATEGY_BRIEF] = ArtifactType.STRATEGY_BRIEF
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    episode_id: _Id
    audience_hypothesis: _Text
    #: 実測（Analytics）に基づくか、仮定か。未検証の流行・米国20代を実測と言わない（設計 §3）
    audience_basis: Literal["measured", "assumed"]
    candidates: tuple[StrategyCandidate, ...] = Field(min_length=1, max_length=5)
    recommended_candidate_id: str = Field(min_length=1, max_length=32)
    success_criteria: tuple[_Text, ...] = Field(default=(), max_length=8)
    citations: tuple[_Short, ...] = Field(default=(), max_length=20)
    uncertainties: tuple[_Text, ...] = Field(default=(), max_length=8)
    lessons_used: tuple[LessonUse, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _consistent(self) -> StrategyBrief:
        ids = [c.id for c in self.candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate ids must be unique")
        chosen = next((c for c in self.candidates if c.id == self.recommended_candidate_id), None)
        if chosen is None:
            raise ValueError("recommended_candidate_id is not a candidate")
        if chosen.status != "proposed":
            raise ValueError("the recommended candidate must be a proposed candidate")
        return self


# --------------------------------------------------------------------------- Creative


class CreativeBrief(_Model):
    """採用した企画の演出指針。台本の ScriptBrief（subject / angle / era / hook）へ渡せる形。"""

    type: Literal[ArtifactType.CREATIVE_BRIEF] = ArtifactType.CREATIVE_BRIEF
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    episode_id: _Id
    adopted_candidate_id: str = Field(min_length=1, max_length=32)
    topic: str = Field(min_length=5, max_length=TOPIC_MAX_CHARS)
    subject: str = Field(pattern=SLUG_PATTERN, max_length=SUBJECT_MAX_CHARS)
    angle: TopicAngle
    era: str = Field(pattern=SLUG_PATTERN, max_length=ERA_MAX_CHARS)
    hook: str = Field(min_length=HOOK_MIN_CHARS, max_length=HOOK_MAX_CHARS)
    strategy_profile_id: str = Field(min_length=1, max_length=64)
    content_profile_id: str = Field(min_length=1, max_length=64)
    hook_options: tuple[
        Annotated[str, StringConstraints(min_length=HOOK_MIN_CHARS, max_length=HOOK_MAX_CHARS)],
        ...,
    ] = Field(min_length=1, max_length=3)
    tone: str = Field(min_length=1, max_length=200)
    visual_style_notes: tuple[_Note, ...] = Field(default=(), max_length=8)
    retention_hypotheses: tuple[_Note, ...] = Field(default=(), max_length=8)
    user_constraints_applied: tuple[_Short, ...] = Field(default=(), max_length=10)


# --------------------------------------------------------------------------- QualityReview


class QualityFinding(_Model):
    id: str = Field(min_length=1, max_length=32)
    severity: Literal["info", "minor", "major", "critical"]
    category: Literal[
        "visual", "narration", "historical_accuracy", "pacing", "audio", "subtitle", "policy"
    ]
    scene_id: str | None = Field(default=None, min_length=1, max_length=16)
    time_range: TimeRange | None = None
    evidence: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0.0, le=1.0)
    #: 修正案は**提案**（ProposeStoryboardChange の visual_description に写る。自動適用しない）
    recommended_visual_description: str | None = Field(default=None, min_length=1, max_length=600)

    @model_validator(mode="after")
    def _range_is_forward(self) -> QualityFinding:
        if self.time_range is not None and self.time_range.end_ms <= self.time_range.start_ms:
            raise ValueError("time_range.end_ms must be greater than start_ms")
        return self


class QualityReview(_Model):
    """品質評価。``assessment_kind`` は常に自己評価で、視聴者データとして扱わない（設計 §3）。"""

    type: Literal[ArtifactType.QUALITY_REVIEW] = ArtifactType.QUALITY_REVIEW
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    episode_id: _Id
    manifest_id: str | None = Field(default=None, min_length=1, max_length=64)
    final_video_revision_id: str | None = Field(default=None, min_length=1, max_length=64)
    findings: tuple[QualityFinding, ...] = Field(default=(), max_length=50)
    overall: Literal["pass", "needs_changes", "blocked_missing_input"]
    assessment_kind: Literal["self_assessed"] = "self_assessed"
    inputs_missing: tuple[_Short, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _consistent(self) -> QualityReview:
        ids = [f.id for f in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("finding ids must be unique")
        if self.overall == "blocked_missing_input" and not self.inputs_missing:
            raise ValueError("blocked_missing_input needs inputs_missing")
        if self.overall == "pass" and any(
            f.severity in ("major", "critical") for f in self.findings
        ):
            raise ValueError("a review with a major or critical finding cannot pass")
        return self


# --------------------------------------------------------------------------- Lesson


class MeasurementWindow(_Model):
    """公開後の測定窓。測れなかった項目は ``None``（0 にしない）で、全て欠けるなら理由が要る。"""

    window: Literal["48h", "7d", "28d"]
    fetched_at: AwareDatetime | None = None
    views: int | None = Field(default=None, ge=0)
    subscribers_gained: int | None = None
    average_view_duration_seconds: float | None = Field(default=None, ge=0)
    average_view_percentage: float | None = Field(default=None, ge=0)
    missing_reason: str | None = Field(default=None, min_length=1, max_length=300)

    @model_validator(mode="after")
    def _reason_when_nothing_was_measured(self) -> MeasurementWindow:
        metrics = (
            self.views,
            self.subscribers_gained,
            self.average_view_duration_seconds,
            self.average_view_percentage,
        )
        if all(m is None for m in metrics) and self.missing_reason is None:
            raise ValueError("a window without any metric needs missing_reason")
        return self


class Lesson(_Model):
    """投稿後の学習。観測と仮説を分け、因果を断定しない（設計 §9）。"""

    type: Literal[ArtifactType.LESSON] = ArtifactType.LESSON
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    episode_id: _Id
    video_id: str = Field(min_length=1, max_length=32)
    manifest_id: str | None = Field(default=None, min_length=1, max_length=64)
    published_at: AwareDatetime | None = None
    measurement_windows: tuple[MeasurementWindow, ...] = Field(default=(), max_length=3)
    observations: tuple[_Text, ...] = Field(default=(), max_length=20)
    hypotheses: tuple[_Text, ...] = Field(default=(), max_length=20)
    alternative_explanations: tuple[_Text, ...] = Field(default=(), max_length=20)
    proposed_experiments: tuple[_Text, ...] = Field(default=(), max_length=10)
    scope: str = Field(min_length=1, max_length=300)
    status: LessonStatus
    comparison_basis: str | None = Field(default=None, min_length=1, max_length=500)
    sample_note: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _windows_are_unique(self) -> Lesson:
        names = [w.window for w in self.measurement_windows]
        if len(set(names)) != len(names):
            raise ValueError("measurement windows must be unique")
        return self


# --------------------------------------------------------------------------- WorkflowPlan


class PlannedStage(_Model):
    stage_id: str = Field(min_length=1, max_length=64)
    handler: str = Field(min_length=1, max_length=64)
    kind: Literal["agent", "existing"]
    after: tuple[str, ...] = ()
    #: agent stage だけが持つ（spec の版と hash を計画に固定する。INV-51）
    agent_role: AgentRole | None = None
    agent_spec_version: str | None = None
    agent_spec_hash: str | None = None
    model_policy_ref: str | None = None
    prompt_version: str | None = None
    timeout_seconds: int | None = Field(default=None, gt=0)
    #: post_publish stage の起動条件
    trigger: str | None = None

    @model_validator(mode="after")
    def _agent_fields_iff_agent(self) -> PlannedStage:
        agent_fields = (
            self.agent_role,
            self.agent_spec_version,
            self.agent_spec_hash,
            self.model_policy_ref,
            self.prompt_version,
            self.timeout_seconds,
        )
        if self.kind == "agent" and any(f is None for f in agent_fields):
            raise ValueError("an agent stage must pin role, spec version, hash and policy")
        if self.kind == "existing" and any(f is not None for f in agent_fields):
            raise ValueError("an existing stage carries no agent fields")
        return self


def compute_plan_hash(fields: dict[str, Any]) -> str:
    """``plan_hash`` を除いた計画の正規 JSON の sha256。"""
    body = {k: v for k, v in fields.items() if k != "plan_hash"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkflowPlan(_Model):
    """Episode 開始時に固定するコンパイル済み実行計画。公開設定・予算上限の項目は持たない。"""

    type: Literal[ArtifactType.WORKFLOW_PLAN] = ArtifactType.WORKFLOW_PLAN
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    template_id: str = Field(min_length=1, max_length=64)
    template_version: str = Field(min_length=1, max_length=16)
    template_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    format_profile: str = Field(min_length=1, max_length=64)
    execution: Literal["legacy_pipeline", "agentic"]
    stages: tuple[PlannedStage, ...] = Field(min_length=1)
    post_publish: tuple[PlannedStage, ...] = ()
    max_qa_repair_cycles: int = Field(ge=0)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _hash_matches(self) -> WorkflowPlan:
        if self.plan_hash != compute_plan_hash(self.model_dump(mode="json")):
            raise ValueError("plan_hash does not match the plan")
        return self


__all__ = [
    "DEFAULT_FORMAT_PROFILE",
    "DEFAULT_TARGET_AUDIENCE",
    "LESSON_TRANSITIONS",
    "SCHEMA_VERSION",
    "CreativeBrief",
    "Lesson",
    "LessonStatus",
    "LessonUse",
    "MeasurementWindow",
    "PlannedStage",
    "QualityFinding",
    "QualityReview",
    "StrategyBrief",
    "StrategyCandidate",
    "UserBrief",
    "WorkflowPlan",
    "compute_plan_hash",
]
