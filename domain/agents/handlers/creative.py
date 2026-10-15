"""creative_director_v1 → CreativeBrief（ADR-0043 §6、設計 §3）。

採用された企画の subject / angle / era / topic は**そのまま写す**（LLM は変えられない）。
LLM が決めるのは hook の選択肢・tone・演出メモ・保持仮説だけ。
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from contracts.agentic import CreativeBrief, StrategyBrief, UserBrief
from contracts.intervention import DecisionCandidate, DecisionRecordPayload, RationaleKind
from domain.agents.handlers._support import ask, build_prompt, derive_model, document, validated
from domain.agents.runtime import AgentInput, AgentOutput, AgentOutputRejected
from domain.agents.tool_gateway import ToolGateway

_Response = derive_model(
    "CreativeResponse",
    CreativeBrief,
    include=("hook_options", "tone", "visual_style_notes", "retention_hypotheses"),
    extra={"rationale": (str | None, Field(default=None, max_length=2000))},
)


async def creative_director_v1(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
    brief = validated(UserBrief, dict(document(agent_input, "user_brief")), "user_brief")
    strategy = validated(
        StrategyBrief, dict(document(agent_input, "strategy_brief")), "strategy_brief"
    )
    decision_in: Any = document(agent_input, "producer_decision")
    adopted_id = decision_in.get("adopted_candidate_id")
    adopted = next((c for c in strategy.candidates if c.id == adopted_id), None)
    if adopted is None or adopted.status != "proposed":
        raise AgentOutputRejected(f"adopted candidate is not a proposed candidate: {adopted_id!r}")

    prompt = build_prompt(
        "Write creative direction for the adopted candidate: 1 to 3 hook options (the first is "
        "used), a tone, visual style notes and retention hypotheses. The subject, angle and era "
        "are fixed; do not change them.",
        {
            "user_brief": brief.model_dump(mode="json"),
            "adopted_candidate": adopted.model_dump(mode="json"),
            "audience_hypothesis": strategy.audience_hypothesis,
        },
    )
    response = await ask(gateway, prompt, _Response)

    creative = validated(
        CreativeBrief,
        {
            "episode_id": agent_input.episode_id,
            "adopted_candidate_id": adopted.id,
            "topic": adopted.title,
            "subject": adopted.subject,
            "angle": adopted.angle,
            "era": adopted.era,
            "hook": response.hook_options[0],
            "strategy_profile_id": brief.strategy_profile_id,
            "content_profile_id": brief.content_profile_id,
            "hook_options": response.hook_options,
            "tone": response.tone,
            "visual_style_notes": response.visual_style_notes,
            "retention_hypotheses": response.retention_hypotheses,
            "user_constraints_applied": brief.must_keep,
        },
        "creative_brief",
    )
    decision = DecisionRecordPayload(
        decision_type="creative_brief",
        question=f"Which hook and direction fit the adopted candidate {adopted.id}?",
        candidates=tuple(
            DecisionCandidate(id=f"hook_{i}", summary=hook)
            for i, hook in enumerate(creative.hook_options, start=1)
        ),
        selected_candidate="hook_1",
        rationale_kind=RationaleKind.REPORTED if response.rationale else RationaleKind.NONE,
        rationale=response.rationale,
        constraints=creative.user_constraints_applied,
    )
    return AgentOutput(artifact=creative, decision=decision, outcome="produced")
