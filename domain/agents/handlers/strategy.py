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
from domain.agents.handlers.tool_use import (
    ToolRound,
    filter_citations,
    plan_and_execute,
    scrub_citations,
    synthesize,
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

#: tool 版（spec 1.1）の合成 prompt に足す指示。1.0 の prompt は変えない
TOOL_SYNTHESIS_TASK = (
    " Tool results are given as DATA blocks named tool:<name>, each with a tool_call_id. Cite "
    "evidence only as tool_call:<tool_call_id> using ids that appear there; never invent ids. A "
    "result whose status is not ok contains no data: do not guess its content and list what could "
    "not be verified under uncertainties. Trend data shows what is trending, not what the US 20s "
    "audience watches; keep the audience a hypothesis unless analytics is marked measured."
)
MAX_CITATIONS = 20
MAX_UNCERTAINTIES = 8

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
    task = (
        f"Propose up to {MAX_CANDIDATES} distinct video topic candidates for the brief. "
        "Each candidate's era must be one of profile.eras. Avoid recent_titles. "
        "Treat lessons with status other than validated as hypotheses."
    )
    data = {
        "user_brief": brief.model_dump(mode="json"),
        "profile": {
            "audience": profile.audience_description,
            "eras": list(profile.eras),
            "angles": [a.value for a in profile.preferred_angles],
        },
        "analytics": analytics,
        "lessons": [i for i in lessons.get("items", ()) if str(i.get("revision_id")) in usable],
        "recent_titles": recent_titles,
    }
    tool_round: ToolRound | None = None
    if agent_input.spec.allowed_tool_ids:
        # spec 1.1: plan -> tool 実行 -> 合成（1 往復。ADR-0047 §3）
        tool_round = await plan_and_execute(gateway, task=task, data=data)
        response = await synthesize(
            gateway,
            task=task + TOOL_SYNTHESIS_TASK,
            data=data,
            tool_round=tool_round,
            model=_Response,
        )
    else:
        response = await ask(gateway, build_prompt(task, data), _Response)

    recent = [normalize_title(t) for t in recent_titles]
    candidates: list[StrategyCandidate] = []
    valid_calls = set(tool_round.tool_call_ids) if tool_round is not None else set()
    dropped_citations = 0
    for proposed in response.candidates:
        fields = proposed.model_dump()
        if tool_round is not None:
            # 理由が引く tool_call は、この run に実在するものだけ（決定論の検査）
            fields["rationale"], n = scrub_citations(proposed.rationale, valid_calls)
            fields["rationale"] = clip(fields["rationale"], 500)
            dropped_citations += n
        reason: str | None = None
        if any(
            jaccard(normalize_title(proposed.title), r) >= DUPLICATE_TITLE_THRESHOLD for r in recent
        ):
            reason = "duplicate_of_recent_title"
        elif proposed.era not in profile.eras:
            reason = "era_not_in_strategy_profile"
        candidates.append(
            StrategyCandidate(
                **fields,
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
    citations = tuple(response.citations)
    rationale = response.rationale
    notes: list[str] = [] if measured else [ASSUMED_AUDIENCE_NOTE]
    if tool_round is not None:
        kept, n = filter_citations(response.citations, valid_calls)
        dropped_citations += n
        if rationale:
            rationale, n = scrub_citations(rationale, valid_calls)
            dropped_citations += n
        # 実行した tool call は全て辿れるように先頭へ（失敗した呼び出しも「参照した根拠」）
        citations = tuple(dict.fromkeys([*tool_round.citations, *kept]))[:MAX_CITATIONS]
        notes.extend(tool_round.notes)
        if dropped_citations:
            notes.append(
                f"{dropped_citations} citation(s) of tool calls that do not exist in this run "
                "were removed."
            )
    uncertainties = [*notes, *response.uncertainties][:MAX_UNCERTAINTIES]
    strategy = validated(
        StrategyBrief,
        {
            "episode_id": agent_input.episode_id,
            "audience_hypothesis": response.audience_hypothesis,
            "audience_basis": "measured" if measured else "assumed",
            "candidates": [c.model_dump() for c in candidates],
            "recommended_candidate_id": recommended,
            "success_criteria": response.success_criteria,
            "citations": citations,
            "uncertainties": uncertainties,
            "lessons_used": [u.model_dump() for u in uses],
        },
        "strategy_brief",
    )
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
        evidence_refs=tuple(
            dict.fromkeys([*strategy.citations, *(tool_round.evidence_refs if tool_round else ())])
        ),
        uncertainties=strategy.uncertainties,
    )
    return AgentOutput(artifact=strategy, decision=decision, outcome="produced")
