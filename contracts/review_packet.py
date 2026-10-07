"""ReviewPacket と変更案ファイル（JP-MAS-001 §8、ADR-0041、``docs/domain/review-packet.md``）。

ReviewPacket は Episode の読み取りモデルを、人（Markdown）と対話する AI（JSON）が読む形に
書き出したもの。**素材は保存先の参照（bucket / object_key / sha256）だけ**で、signed URL・token を
入れない。分からないこと・確かめていないことは ``missing`` に列挙する（補完しない）。

``ChangeProposalFile`` は ReviewPacket を読んだ人・AI が作る変更案の取り込み形式。各 proposal は
``ProposeChangePayload`` そのもの（基準 revision は ``change.base_revision_id``、基準 manifest は
``base_manifest_id``）。``suggested_command_ids`` は作成側の控えで、API は自分で決定的な
command_id を導く（``docs/operations/intervention-api.md``）。

JSON Schema は ``docs/domain/review-packet.schema.json`` に書き出し、
``tests/contract/test_review_packet_contract.py`` が一致を検査する（再生成は
``python -m contracts.review_packet``）。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from contracts.intervention import ProposeChangePayload

REVIEW_PACKET_SCHEMA_VERSION = "1.0"
CHANGE_PROPOSAL_FILE_SCHEMA_VERSION = "1.0"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PacketControl(_Frozen):
    mode: str
    control_state: str
    control_revision: int
    review_points: list[str]
    waiting_point: str | None
    waiting_manifest_id: str | None
    #: False = 制御行が無く既定値（legacy / running）で読んだ
    persisted: bool


class PacketEpisodeState(_Frozen):
    episode_id: str
    #: 既存の ``episodes.status``
    phase: str
    topic: str | None
    topic_plan_id: str | None
    blocked_reason: str | None
    control: PacketControl


class PacketRevision(_Frozen):
    """採用 manifest の member（対象 revision）。"""

    slot: str
    revision_id: str
    artifact_type: str | None
    scene_id: str | None
    version: int | None
    stale: bool
    stale_reasons: list[str]


class PacketCandidate(_Frozen):
    id: str
    summary: str
    benefits: list[str]
    concerns: list[str]
    scores: dict[str, float] | None
    assessment_kind: str | None
    self_assessed_scores: dict[str, float] | None
    rejected_reason: str | None


class PacketRationale(_Frozen):
    """判断の理由。種別（reported / post_hoc_summary / human / rule / none）を必ず伴う。

    tool 呼び出し・結果の参照とは混ぜない（``PacketDecision.tool_invocation_refs``）。
    """

    kind: str
    text: str | None


class PacketDecision(_Frozen):
    decision_id: str | None
    decision_type: str
    #: True = 当時の記録ではなく、保存済みデータから読み取り時に導いた
    derived: bool
    actor_type: str
    actor_id: str
    question: str | None
    candidates: list[PacketCandidate]
    selected_candidate: str | None
    rationale: PacketRationale
    evidence_refs: list[str]
    evidence_check: str | None
    tool_invocation_refs: list[str]
    input_snapshot_ref: str | None
    target_revisions: list[str]
    result_revision: str | None
    uncertainties: list[str]


class PacketScriptSegment(_Frozen):
    segment_id: str
    narration: str
    visual: str
    duration_ms: int


class PacketScript(_Frozen):
    revision_id: str
    title: str
    hook: str
    language: str
    total_duration_ms: int
    segments: list[PacketScriptSegment]


class PacketEvidence(_Frozen):
    research_artifact_id: str
    artifact_type: str
    request_id: str
    bucket: str
    object_key: str
    sha256: str
    is_current: bool
    verdict: str | None
    script_revision_id: str | None


class PacketComment(_Frozen):
    """コメント・レビュー所見・承認・制約。古い版へのコメントはアンカーを動かさず印を付ける。"""

    annotation_id: str
    kind: str
    status: str
    old_version: bool
    approval_current: bool | None
    anchor: dict[str, Any]
    body: str
    actor_type: str
    actor_id: str
    manifest_id: str | None
    created_at: str


class PacketCommand(_Frozen):
    command_id: str
    type: str
    status: str
    #: ``applied`` のときだけ True（受付は完了ではない）
    done: bool
    display_status: str


class PacketChangeSet(_Frozen):
    change_set_id: str
    kind: str
    status: str
    base_revision_id: str
    actor_type: str
    estimated_cost_usd: str | None
    impact: dict[str, Any] | None


class PacketUnmeasured(_Frozen):
    provider: str
    calls: int


class PacketCosts(_Frozen):
    provider_usd: str
    research_usd: str
    measured_usd: str
    unmeasured: list[PacketUnmeasured]
    #: LLM の費用は記録が無い。0 ではない
    llm: Literal["unmeasured"]


class PacketReservation(_Frozen):
    reservation_id: str
    provider: str
    scene_id: str | None
    estimated_cost_usd: str | None


class PacketReservations(_Frozen):
    in_flight: list[PacketReservation]
    unknown_outcome: list[PacketReservation]
    undispatched: list[PacketReservation]


class PacketAssetRef(_Frozen):
    """保存先の参照だけ。signed URL・token を持たない。"""

    revision_id: str
    slot: str
    artifact_type: str | None
    bucket: str | None
    object_key: str | None
    sha256: str | None


class PacketMissingItem(_Frozen):
    item: str
    reason: str


class ReviewPacket(_Frozen):
    schema_version: Literal["1.0"] = REVIEW_PACKET_SCHEMA_VERSION
    episode_id: str
    #: 採用 manifest（無ければ None。``manifest_persisted=false`` で現行の成果物から組んだ）
    manifest_id: str | None
    manifest_persisted: bool
    episode: PacketEpisodeState
    target_revisions: list[PacketRevision]
    decisions: list[PacketDecision]
    script: PacketScript | None
    evidence: list[PacketEvidence]
    reviews: list[PacketComment]
    stale_approvals: list[str]
    commands: list[PacketCommand]
    change_sets: list[PacketChangeSet]
    costs: PacketCosts
    open_reservations: PacketReservations
    asset_refs: list[PacketAssetRef]
    #: 欠けている・確かめていない事柄（補完しない）
    missing: list[PacketMissingItem]


class ChangeProposalFile(_Frozen):
    """ReviewPacket から作った変更案の取り込み形式。"""

    schema_version: Literal["1.0"] = CHANGE_PROPOSAL_FILE_SCHEMA_VERSION
    #: 対象の Episode（API は URL の Episode と一致しなければ拒否する）
    episode_id: str | None = None
    #: 案を作るときに読んだ ReviewPacket の manifest（各 proposal の基準 manifest と一致すること）
    base_manifest_id: str | None = None
    proposals: list[ProposeChangePayload] = Field(min_length=1)
    #: 作成側が控えた command_id（``proposals`` と同じ順。API は自分で決定的な id を導く）
    suggested_command_ids: list[str] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ChangeProposalFile:
        if self.base_manifest_id is not None:
            for index, proposal in enumerate(self.proposals):
                if proposal.base_manifest_id not in (None, self.base_manifest_id):
                    raise ValueError(
                        f"proposal {index} is based on manifest {proposal.base_manifest_id}, "
                        f"not {self.base_manifest_id}"
                    )
        if self.suggested_command_ids is not None:
            if len(self.suggested_command_ids) != len(self.proposals):
                raise ValueError("suggested_command_ids must align with proposals")
            if any(not (8 <= len(c) <= 64) for c in self.suggested_command_ids):
                raise ValueError("a command_id is 8..64 characters")
        return self


def review_packet_json_schema() -> dict[str, Any]:
    """``docs/domain/review-packet.schema.json`` の中身（ReviewPacket と変更案ファイル）。"""
    return {
        "$comment": "generated by `python -m contracts.review_packet`; do not edit by hand",
        "review_packet": ReviewPacket.model_json_schema(),
        "change_proposal_file": ChangeProposalFile.model_json_schema(),
    }


def render_json_schema() -> str:
    return json.dumps(review_packet_json_schema(), ensure_ascii=False, indent=2, sort_keys=True) + (
        "\n"
    )


if __name__ == "__main__":  # pragma: no cover - 書き出し用
    from pathlib import Path

    Path("docs/domain/review-packet.schema.json").write_text(render_json_schema(), encoding="utf-8")
