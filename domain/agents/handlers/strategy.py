"""strategy_agent_v1 → StrategyBrief（ADR-0043 §6、設計 §3）。

LLM は候補を**提案**するだけ。重複の除外・era の検査・audience_basis・lessons_used は
決定論で決める。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from contracts.agentic import LessonStatus, LessonUse, StrategyBrief, StrategyCandidate, UserBrief
from contracts.intervention import DecisionCandidate, DecisionRecordPayload, RationaleKind
from contracts.topic_planning import DEFAULT_PLANNER_POLICY, STRATEGY_PROFILES
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
from domain.topic_planning.normalize import jaccard, normalize_title

#: 「実測」と呼ぶのに要る、views のある動画の最小本数。これ未満は仮定（米国20代を実測と言わない）
MIN_VIDEOS_FOR_MEASURED_AUDIENCE = 10
#: 近い重複とみなす title 類似度。既存 Planner の reject 閾値と同じ（定義は PlannerPolicy が持つ）
DUPLICATE_TITLE_THRESHOLD = DEFAULT_PLANNER_POLICY.similarity_reject
MAX_CANDIDATES = 5

ASSUMED_AUDIENCE_NOTE = (
    "The fit with the US 20s audience is assumed, not measured: there is not enough channel "
    "analytics to verify it."
)

_ProposedCandidate = derive_model(
    "ProposedStrategyCandidate", StrategyCandidate, exclude=("status", "rejected_reason")
)
_Response = derive_model(
    "StrategyResponse",
    StrategyBrief,
    include=(
        "audience_hypothesis",
        "success_criteria",
        "citations",
        "uncertainties",
    ),
    extra={
        "candidates": (
            tuple[_ProposedCandidate, ...],
            Field(min_length=1, max_length=MAX_CANDIDATES),
        ),
        "recommended_candidate_id": (str | None, None),
        "rationale": (str | None, Field(default=None, max_length=2000)),
    },
)


def _lessons_used(lessons: dict[str, Any]) -> tuple[LessonUse, ...]:
    uses: list[LessonUse] = []
    for item in lessons.get("items", ()):
        try:
            status = LessonStatus(item["status"])
            revision = str(item["revision_id"])
        except (KeyError, ValueError) as exc:
            raise AgentOutputRejected(f"malformed lesson input: {item!r}") from exc
        if status in (LessonStatus.REJECTED, LessonStatus.RETIRED):
            continue
        used_as = "validated_reference" if status is LessonStatus.VALIDATED else "hypothesis"
        uses.append(LessonUse(lesson_revision_id=revision, lesson_status=status, used_as=used_as))
    uses.sort(key=lambda u: u.used_as != "validated_reference")  # 検証済みを先に（上限 10 件）
    return tuple(uses[:10])


def _is_measured(analytics: dict[str, Any]) -> bool:
    videos = analytics.get("videos_with_views")
    return (
        analytics.get("measured") is True
        and isinstance(videos, int)
        and not isinstance(videos, bool)
        and videos >= MIN_VIDEOS_FOR_MEASURED_AUDIENCE
    )


async def strategy_agent_v1(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
    brief = validated(UserBrief, dict(document(agent_input, "user_brief")), "user_brief")
    profile = STRATEGY_PROFILES.get(brief.strategy_profile_id)
    if profile is None:
        raise AgentOutputRejected(f"unknown strategy profile: {brief.strategy_profile_id!r}")
    analytics = dict(document(agent_input, "analytics"))
    lessons = dict(document(agent_input, "lessons"))
    recent_titles = [str(t) for t in document(agent_input, "recent_titles").get("titles", ())]
    uses = _lessons_used(lessons)
    usable = {u.lesson_revision_id for u in uses}
    prompt = build_prompt(
        f"Propose up to {MAX_CANDIDATES} distinct video topic candidates for the brief. "
        "Each candidate's era must be one of profile.eras. Avoid recent_titles. "
        "Treat lessons with status other than validated as hypotheses.",
        {
            "user_brief": brief.model_dump(mode="json"),
            "profile": {
                "audience": profile.audience_description,
                "eras": list(profile.eras),
                "angles": [a.value for a in profile.preferred_angles],
            },
            "analytics": analytics,
            "lessons": [i for i in lessons.get("items", ()) if str(i.get("revision_id")) in usable],
            "recent_titles": recent_titles,
        },
    )
    response = await ask(gateway, prompt, _Response)

    recent = [normalize_title(t) for t in recent_titles]
    candidates: list[StrategyCandidate] = []
    for proposed in response.candidates:
        data = proposed.model_dump()
        reason: str | None = None
        if any(
            jaccard(normalize_title(proposed.title), r) >= DUPLICATE_TITLE_THRESHOLD for r in recent
        ):
            reason = "duplicate_of_recent_title"
        elif proposed.era not in profile.eras:
            reason = "era_not_in_strategy_profile"
        candidates.append(
            StrategyCandidate(
                **data,
                status="rejected" if reason else "proposed",
                rejected_reason=reason,
            )
        )
    proposed_ids = [c.id for c in candidates if c.status == "proposed"]
    if not proposed_ids:
        raise AgentOutputRejected("no candidate survived the deterministic checks")
    wanted = response.recommended_candidate_id
    recommended = wanted if wanted in proposed_ids else proposed_ids[0]

    measured = _is_measured(analytics)
    uncertainties = list(response.uncertainties)
    if not measured:
        uncertainties = [ASSUMED_AUDIENCE_NOTE, *uncertainties][:8]
    strategy = validated(
        StrategyBrief,
        {
            "episode_id": agent_input.episode_id,
            "audience_hypothesis": response.audience_hypothesis,
            "audience_basis": "measured" if measured else "assumed",
            "candidates": [c.model_dump() for c in candidates],
            "recommended_candidate_id": recommended,
            "success_criteria": response.success_criteria,
            "citations": response.citations,
            "uncertainties": uncertainties,
            "lessons_used": [u.model_dump() for u in uses],
        },
        "strategy_brief",
    )
    rationale = response.rationale
    decision = DecisionRecordPayload(
        decision_type="strategy_brief",
        question="Which topic should this episode make?",
        candidates=tuple(
            DecisionCandidate(
                id=c.id,
                summary=clip(c.title, 2000),
                benefits=(c.rationale,),
                concerns=c.risks,
                rejected_reason=c.rejected_reason,
            )
            for c in strategy.candidates
        ),
        selected_candidate=recommended,
        rationale_kind=RationaleKind.REPORTED,
        rationale=rationale,
        evidence_refs=strategy.citations,
        uncertainties=strategy.uncertainties,
    )
    return AgentOutput(artifact=strategy, decision=decision, outcome="produced")
