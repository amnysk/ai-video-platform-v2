"""Learning を測定が揃うまで何度でも起動できる（``idempotency_scope``、P1-D の発見）。

同じ Episode・同じ計画の Learning stage は冪等キーが同じなので、``awaiting_public_metrics`` の
記録が replay され続け、公開後に起動し直しても Lesson ができなかった。実 PostgreSQL の E2E で露見。
各テストの理由: docs/testing/jpmas2-p1d-rationale.md
"""

from __future__ import annotations

import dataclasses

from sqlalchemy import func, select

from contracts.agentic import Lesson
from contracts.agentic_pipeline import AgentStageOutcome, agent_stage_idempotency_key
from contracts.agents import AgentHandlerRef
from contracts.intervention import AgentRole
from contracts.states import ArtifactType
from domain.agents.runtime import AgentHandler, AgentInput, AgentOutput
from domain.agents.tool_gateway import ToolGateway
from infrastructure.db.models import AgentRunRow
from infrastructure.db.repositories import ArtifactMetadataRepository
from tests.support.agent_stage import decision, make_world, stage_request

LEARNING = AgentHandlerRef.LEARNING_AGENT_V1


def test_the_key_without_a_scope_is_unchanged_and_a_scope_makes_a_different_key() -> None:
    base = agent_stage_idempotency_key("e", "lesson", "h")
    assert base == "agentic:e:lesson:h"
    assert agent_stage_idempotency_key("e", "lesson", "h", None) == base
    scoped = agent_stage_idempotency_key("e", "lesson", "h", "run-1")
    assert scoped == "agentic:e:lesson:h:run-1" and scoped != base


def _learning_handler(measured: list[bool]) -> AgentHandler:
    async def handler(agent_input: AgentInput, gateway: ToolGateway) -> AgentOutput:
        if not measured[0]:
            return AgentOutput(
                artifact=None, decision=None, outcome="awaiting_public_metrics", note="private"
            )
        lesson = Lesson.model_validate(
            {
                "episode_id": agent_input.episode_id,
                "video_id": "abcdefghijk",
                "scope": "This video only.",
                "status": "proposed",
            }
        )
        return AgentOutput(artifact=lesson, decision=decision("lesson"), outcome="produced")

    return handler


async def test_a_later_run_with_a_new_scope_can_produce_the_lesson_after_an_awaiting_run(
    session_factory, artifact_store
) -> None:
    """awaiting の記録は同じ scope では replay、新しい scope（= 新しい起動）では測り直す。"""
    world = await make_world(session_factory, artifact_store)
    measured = [False]
    activities = world.activities({LEARNING: _learning_handler(measured)})
    request, _ = stage_request(AgentRole.LEARNING, world.episode_id, stage_id="lesson")

    first = await activities.run_agent_stage(request)
    assert first.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value

    measured[0] = True
    same_scope = await activities.run_agent_stage(request)  # Temporal の再試行は同じ結果
    assert same_scope.replayed and same_scope.outcome == first.outcome

    later = await activities.run_agent_stage(
        dataclasses.replace(request, idempotency_scope="run-2")
    )
    assert later.outcome == AgentStageOutcome.PRODUCED.value and not later.replayed
    async with session_factory() as session:
        lesson = await ArtifactMetadataRepository(session).find_current_by_type(
            world.episode_id, ArtifactType.LESSON
        )
        runs = await session.scalar(select(func.count()).select_from(AgentRunRow))
    assert lesson is not None and str(lesson.id) == later.artifact_revision_id
    assert runs == 2  # awaiting の 1 件 + 測れた 1 件
