"""quality_reviewer_v1 → QualityReview（ADR-0043 §6、設計 §3）。

- 入力が欠けるなら LLM を呼ばず ``blocked_missing_input``
- LLM の所見は決定論で検証する（存在しない scene・動画の尺を超える範囲は捨て、理由を残す）
- 規則レビュー（``rule_review``）は文脈として渡すだけ。所見に混ぜない
- 修正案は ``StoryboardPatchProposal``（visual_description のみ・最大 3 edit）の
  **提案**で、適用しない（INV-57）
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field, ValidationError

from contracts.agentic import QualityFinding, QualityReview
from contracts.application import StoryboardPatchProposal, StoryboardSceneEdit
from contracts.intervention import (
    AssessmentKind,
    DecisionCandidate,
    DecisionRecordPayload,
    RationaleKind,
)
from domain.agents.handlers._support import (
    ask,
    build_prompt,
    clip,
    derive_model,
    document,
    validated,
)
from domain.agents.runtime import AgentInput, AgentOutput, AgentOutputRejected
from domain.agents.tool_gateway import ToolGateway

#: 1 つの提案に入れる edit の上限
MAX_PROPOSAL_EDITS = 3

_SEVERITY_RANK = {"critical": 0, "major": 1}

_Response = derive_model(
    "QualityResponse",
    QualityReview,
    include=(),
    extra={
        "findings": (list[dict[str, Any]], Field(default_factory=list, max_length=50)),
        "rationale": (str | None, Field(default=None, max_length=2000)),
    },
)


def _missing(documents: dict[str, Any]) -> list[str]:
    return [name for name in ("final_video", "storyboard") if not documents[name]]


def _duration_ms(storyboard: dict[str, Any], final_video: dict[str, Any]) -> int:
    for source in (final_video.get("duration_ms"), storyboard.get("total_duration_ms")):
        if isinstance(source, int) and not isinstance(source, bool) and source > 0:
            return source
    ends = [s["start_ms"] + s["duration_ms"] for s in storyboard.get("scenes", ())]
    return max(ends, default=0)


def _decision(
    review: QualityReview,
    kept: tuple[QualityFinding, ...],
    rationale: str | None,
    kind: RationaleKind,
) -> DecisionRecordPayload:
    if kept:
        candidates = tuple(
            DecisionCandidate(
                id=f.id,
                summary=clip(f.evidence, 2000),
                concerns=(f"{f.severity}:{f.category}",),
                scores={"confidence": f.confidence},
                assessment_kind=AssessmentKind.SELF_ASSESSED,
            )
            for f in kept
        )
    else:
        candidates = (DecisionCandidate(id=review.overall, summary=f"overall: {review.overall}"),)
    return DecisionRecordPayload(
        decision_type="quality_review",
        question="Does the rendered video meet the quality bar?",
        candidates=candidates,
        rationale_kind=kind,
        rationale=rationale,
        uncertainties=(),
    )


async def quality_reviewer_v1(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
    docs: dict[str, Any] = {
        name: dict(document(agent_input, name))
        for name in ("script", "storyboard", "final_video", "rule_review", "manifest")
    }
    missing = _missing(docs)
    if missing:
        review = QualityReview(
            episode_id=agent_input.episode_id,
            manifest_id=docs["manifest"].get("manifest_id"),
            final_video_revision_id=docs["final_video"].get("revision_id"),
            overall="blocked_missing_input",
            inputs_missing=tuple(missing),
        )
        decision = DecisionRecordPayload(
            decision_type="quality_review",
            question="Does the rendered video meet the quality bar?",
            candidates=(
                DecisionCandidate(
                    id="blocked_missing_input", summary="missing: " + ", ".join(missing)
                ),
            ),
            rationale_kind=RationaleKind.RULE,
            rationale="Required inputs are missing, so no review was attempted.",
            constraints=tuple(missing),
        )
        return AgentOutput(
            artifact=review,
            decision=decision,
            outcome="produced",
            note="blocked_missing_input",
        )

    storyboard, final_video = docs["storyboard"], docs["final_video"]
    scene_ids = {s.get("scene_id") for s in storyboard.get("scenes", ())}
    duration_ms = _duration_ms(storyboard, final_video)
    prompt = build_prompt(
        "Review the rendered video against the storyboard and script. Report findings with "
        "severity, category, scene_id, time_range (milliseconds within the video), evidence and "
        "confidence. For a major or critical visual problem you may include a "
        "recommended_visual_description. rule_review is context only.",
        {k: docs[k] for k in ("script", "storyboard", "final_video", "rule_review")},
    )
    response = await ask(gateway, prompt, _Response)

    kept: list[QualityFinding] = []
    dropped: list[str] = []
    for raw in response.findings:
        try:
            finding = QualityFinding.model_validate(raw)
        except ValidationError as exc:
            raise AgentOutputRejected(f"LLM finding violates QualityFinding: {exc}") from exc
        if finding.scene_id is not None and finding.scene_id not in scene_ids:
            dropped.append(f"{finding.id}: scene {finding.scene_id} is not in the storyboard")
        elif finding.time_range is not None and finding.time_range.end_ms > duration_ms:
            dropped.append(
                f"{finding.id}: time_range exceeds the video duration ({duration_ms} ms)"
            )
        else:
            kept.append(finding)
    if len({f.id for f in kept}) != len(kept):
        raise AgentOutputRejected("finding ids must be unique")

    blocking = any(f.severity in ("major", "critical") for f in kept)
    review = validated(
        QualityReview,
        {
            "episode_id": agent_input.episode_id,
            "manifest_id": docs["manifest"].get("manifest_id"),
            "final_video_revision_id": final_video.get("revision_id"),
            "findings": [f.model_dump() for f in kept],
            "overall": "needs_changes" if blocking else "pass",
        },
        "quality_review",
    )
    proposals = _proposals(agent_input, docs, review.findings)
    decision = _decision(
        review,
        review.findings,
        response.rationale,
        RationaleKind.REPORTED if response.rationale else RationaleKind.NONE,
    )
    uncertainties = tuple(clip(f"dropped finding {d}", 2000) for d in dropped)
    decision = decision.model_copy(update={"uncertainties": uncertainties})
    return AgentOutput(
        artifact=review,
        decision=decision,
        proposals=proposals,
        findings=review.findings,
        outcome="produced",
    )


def _proposals(
    agent_input: AgentInput, docs: dict[str, Any], findings: tuple[QualityFinding, ...]
) -> tuple[StoryboardPatchProposal, ...]:
    revision = docs["storyboard"].get("revision_id")
    if not revision:
        return ()
    ranked = sorted(
        (
            f
            for f in findings
            if f.severity in _SEVERITY_RANK and f.scene_id and f.recommended_visual_description
        ),
        key=lambda f: (_SEVERITY_RANK[f.severity], -f.confidence, f.id),
    )
    edits: list[StoryboardSceneEdit] = []
    seen: set[str] = set()
    for f in ranked:
        if len(edits) >= MAX_PROPOSAL_EDITS:
            break
        assert f.scene_id is not None
        if f.scene_id in seen:
            continue
        try:
            edit = StoryboardSceneEdit(
                scene_id=f.scene_id, visual_description=f.recommended_visual_description
            )
        except ValidationError:
            continue
        seen.add(f.scene_id)
        edits.append(edit)
    if not edits:
        return ()
    try:
        episode_id = uuid.UUID(agent_input.episode_id)
    except ValueError as exc:
        raise AgentOutputRejected("episode_id is not a UUID") from exc
    return (
        StoryboardPatchProposal(
            episode_id=episode_id,
            base_manifest_id=docs["manifest"].get("manifest_id"),
            base_storyboard_revision_id=str(revision),
            edits=tuple(edits),
            rationale="Quality review findings: " + ", ".join(f.id for f in ranked[: len(edits)]),
        ),
    )
