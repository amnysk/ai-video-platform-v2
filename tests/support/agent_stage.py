"""agent worker（run_agent_stage）のテスト支援。SQLite + InMemoryArtifactStore + 偽物。

handler は P1-A の本物ではなく、gateway 経由で LLM を 1 回呼んで契約どおりの出力を返す
最小の偽物（worker の責務＝入力・保存・予算・提案の配線だけを試す）。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from contracts.agentic import (
    CreativeBrief,
    PlannedStage,
    QualityFinding,
    QualityReview,
    StrategyBrief,
    UserBrief,
)
from contracts.agentic_pipeline import RunAgentStageRequest
from contracts.agents import AgentHandlerRef, AgentSpec, resolve_agent_spec
from contracts.application import StoryboardPatchProposal, StoryboardSceneEdit
from contracts.intervention import (
    AgentRole,
    DecisionCandidate,
    DecisionRecordPayload,
    RationaleKind,
)
from contracts.states import ArtifactType, EpisodeStatus
from domain.agents.ports import LlmResult
from domain.agents.runtime import AgentHandler, AgentInput, AgentOutput, AgentOutputRejected
from domain.agents.tool_gateway import ToolGateway
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.internal_port import SessionCommandPort
from tests.support.intervention import make_episode, seed_script_and_storyboard, store_artifact
from workers.agents.activities import AgentActivities

PLAN_HASH = "a" * 64
RATE = Decimal("150")


class CountingLlm:
    """決定的な偽 LLM。呼び出し回数と使用量を持つ（既定は token を返す）。"""

    def __init__(self, *, tokens: tuple[int | None, int | None] = (100, 50)) -> None:
        self.calls = 0
        self._tokens = tokens

    async def generate_json(
        self, *, prompt: str, schema: dict[str, Any], max_tokens: int
    ) -> LlmResult:
        self.calls += 1
        return LlmResult(
            data={"ok": True, "n": self.calls},
            model="fake-model",
            input_tokens=self._tokens[0],
            output_tokens=self._tokens[1],
        )


@dataclass
class FakeStarter:
    started: list[str] = field(default_factory=list)

    async def start(self, command_id: str) -> None:
        self.started.append(command_id)


def candidate(id_: str = "c1") -> dict[str, Any]:
    return {
        "id": id_,
        "title": "Why the Shogun hid in plain sight",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
        "rationale": "Strong curiosity gap.",
        "status": "proposed",
    }


def user_brief() -> dict[str, Any]:
    return UserBrief(
        theme="Edo power", template_id="shorts_agentic", template_version="1"
    ).model_dump(mode="json")


def strategy_brief(episode_id: str) -> dict[str, Any]:
    return StrategyBrief.model_validate(
        {
            "episode_id": episode_id,
            "audience_hypothesis": "US 20s like power-structure stories.",
            "audience_basis": "assumed",
            "candidates": [candidate("c1"), candidate("c2")],
            "recommended_candidate_id": "c1",
        }
    ).model_dump(mode="json")


def decision(kind: str) -> DecisionRecordPayload:
    return DecisionRecordPayload(
        decision_type=kind,
        question=f"{kind}?",
        candidates=(DecisionCandidate(id="x", summary="the output"),),
        selected_candidate="x",
        rationale_kind=RationaleKind.REPORTED,
        rationale="because",
    )


def strategy_handler() -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        await gateway.llm_json("propose candidates", {"type": "object"})
        brief = StrategyBrief.model_validate(strategy_brief(agent_input.episode_id))
        return AgentOutput(artifact=brief, decision=decision("strategy_brief"), outcome="produced")

    return handler


def creative_handler() -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        await gateway.llm_json("direct", {"type": "object"})
        creative = CreativeBrief.model_validate(
            {
                "episode_id": agent_input.episode_id,
                "adopted_candidate_id": "c1",
                "topic": "Why the Shogun hid in plain sight",
                "subject": "tokugawa_ieyasu",
                "angle": "reason",
                "era": "edo",
                "hook": "He ruled Japan without ever being king.",
                "strategy_profile_id": "us_young_history_v1",
                "content_profile_id": "japan_history_shorts_v1",
                "hook_options": ["He ruled Japan without ever being king."],
                "tone": "dry wit",
            }
        )
        return AgentOutput(
            artifact=creative, decision=decision("creative_brief"), outcome="produced"
        )

    return handler


def quality_handler(
    *, episode_id: str, storyboard_revision_id: str, with_proposal: bool = True
) -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        await gateway.llm_json("review", {"type": "object"})
        finding = QualityFinding(
            id="f1",
            severity="major",
            category="visual",
            scene_id="sb1",
            time_range={"start_ms": 0, "end_ms": 1500},  # type: ignore[arg-type]
            evidence="The map is unreadable.",
            confidence=0.7,
            recommended_visual_description="A clear aerial view of the castle town",
        )
        review = QualityReview(
            episode_id=agent_input.episode_id,
            manifest_id=None,
            findings=(finding,),
            overall="needs_changes",
        )
        proposals = ()
        if with_proposal:
            proposals = (
                StoryboardPatchProposal(
                    episode_id=uuid.UUID(episode_id),
                    base_storyboard_revision_id=storyboard_revision_id,
                    edits=(
                        StoryboardSceneEdit(
                            scene_id="sb1",
                            visual_description="A clear aerial view of the castle town",
                        ),
                    ),
                    rationale="make the map readable",
                ),
            )
        return AgentOutput(
            artifact=review,
            decision=decision("quality_review"),
            proposals=proposals,
            findings=(finding,),
            outcome="produced",
        )

    return handler


def awaiting_handler() -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        return AgentOutput(
            artifact=None,
            decision=None,
            outcome="awaiting_public_metrics",
            note="the video is private; no public metrics yet",
        )

    return handler


def rejecting_handler() -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        await gateway.llm_json("propose", {"type": "object"})
        raise AgentOutputRejected("LLM output violates StrategyResponse")

    return handler


def wrong_artifact_handler() -> AgentHandler:
    """strategy の spec なのに creative_brief を返す（spec の output_schema_ref と合わない）。"""
    inner = creative_handler()

    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        return await inner(agent_input, gateway)

    return handler


def stage_request(
    role: AgentRole, episode_id: str, *, stage_id: str | None = None
) -> tuple[RunAgentStageRequest, AgentSpec]:
    spec = resolve_agent_spec(role, "1.0")
    stage = PlannedStage(
        stage_id=stage_id or role.value,
        handler=spec.handler_ref.value,
        kind="agent",
        agent_role=role,
        agent_spec_version=spec.spec_version,
        agent_spec_hash=spec.manifest_hash,
        model_policy_ref=spec.model_policy_ref,
        prompt_version=spec.prompt_version,
        timeout_seconds=spec.timeout_seconds,
    )
    return (
        RunAgentStageRequest(
            episode_id=episode_id,
            stage=stage.model_dump(mode="json"),
            plan_hash=PLAN_HASH,
            template_id="shorts_agentic",
            template_version="1",
        ),
        spec,
    )


@dataclass
class World:
    session_factory: Any
    store: Any
    episode_id: str
    starter: FakeStarter
    llm: CountingLlm
    storyboard_revision_id: str | None = None

    def activities(
        self,
        handlers: Mapping[AgentHandlerRef, AgentHandler],
        *,
        enabled: frozenset[AgentRole] | None = None,
        agentic_enabled: bool = True,
        rate: Decimal | None = RATE,
        **budget: Any,
    ) -> AgentActivities:
        return AgentActivities(
            session_factory=self.session_factory,
            store=self.store,
            bucket="artifacts",
            agentic_enabled=agentic_enabled,
            enabled_roles=frozenset(AgentRole) if enabled is None else enabled,
            budget_policy=BudgetPolicy(usd_jpy_rate=rate, **budget),
            llm_factory=lambda _e, _s, _p: self.llm,
            command_port=SessionCommandPort(self.session_factory, self.starter),
            handlers=handlers,
        )


async def make_world(
    session_factory: Any,
    store: Any,
    *,
    brief: bool = True,
    strategy: bool = False,
    storyboard: bool = False,
    llm: CountingLlm | None = None,
) -> World:
    async with session_factory() as session:
        episode_id = await make_episode(
            session, EpisodeStatus.ASSETS_READY if storyboard else EpisodeStatus.PLANNED
        )
        if brief:
            await store_artifact(session, store, episode_id, ArtifactType.USER_BRIEF, user_brief())
        if strategy:
            await store_artifact(
                session,
                store,
                episode_id,
                ArtifactType.STRATEGY_BRIEF,
                strategy_brief(episode_id),
            )
        revision = None
        if storyboard:
            _script, board, _media = await seed_script_and_storyboard(session, store, episode_id)
            revision = board.id
    return World(session_factory, store, episode_id, FakeStarter(), llm or CountingLlm(), revision)


async def adopt_candidate(session_factory: Any, episode_id: str, candidate_id: str = "c1") -> None:
    """producer が候補を採用した判断記録（``choose_topic``）。"""
    from contracts.intervention import ActorType
    from infrastructure.intervention.store import Actor, InterventionStore

    async with session_factory() as session:
        await InterventionStore(session).insert_decision(
            idempotency_key=f"test-choose-topic:{episode_id}",
            decision_type="choose_topic",
            actor=Actor(ActorType.SYSTEM, "producer"),
            payload=decision("choose_topic")
            .model_copy(update={"selected_candidate": candidate_id})
            .model_dump(mode="json"),
            episode_id=episode_id,
        )
        await session.commit()
