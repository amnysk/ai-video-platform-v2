"""AgenticEpisodeWorkflow を同じ id で再起動しても、完了済みの工程をやり直さない。

hold/resume で `AgenticEpisodeWorkflow` を再起動する（ADR-0043 §Consequences の解消）。
再起動は同じ計画を上から歩き直す。LLM の二重呼び出し・予算の二重計上・成果物の二重書き込みが
無いことを証明する。
各テストの理由: docs/testing/jpmas2-fixa-rationale.md
"""

from __future__ import annotations

from sqlalchemy import func, select

from contracts.agentic_pipeline import (
    AgentStageOutcome,
    PinWorkflowPlanRequest,
)
from contracts.agents import AgentHandlerRef
from contracts.intervention import AgentRole
from contracts.states import ArtifactType
from infrastructure.db.models import ProviderReservationRow
from infrastructure.db.repositories import ArtifactMetadataRepository
from tests.support.agent_stage import make_world, stage_request, strategy_handler
from tests.unit.test_agent_stage_activity import _snapshot
from workers.pipeline.agentic_activities import AgenticPipelineActivities

STRATEGY = AgentHandlerRef.STRATEGY_AGENT_V1


async def test_a_restart_returns_the_stored_stage_without_a_new_llm_call_or_reservation(
    session_factory, artifact_store
) -> None:
    world = await make_world(session_factory, artifact_store)
    request, _ = stage_request(AgentRole.STRATEGY, world.episode_id)
    first = await world.activities({STRATEGY: strategy_handler()}).run_agent_stage(request)
    assert first.outcome == AgentStageOutcome.PRODUCED.value
    before = await _snapshot(session_factory)
    assert before["provider_reservations"] == 1 and world.llm.calls == 1

    # 再起動 = 新しい worker / 新しい実行が、同じ計画から同じ要求（scope なし）を出す
    restarted = world.activities({STRATEGY: strategy_handler()})
    again = await restarted.run_agent_stage(request)

    assert again.replayed is True and again.artifact_revision_id == first.artifact_revision_id
    assert await _snapshot(session_factory) == before  # 予約・成果物・判断記録・run の増加なし
    assert world.llm.calls == 1


async def test_pinning_again_returns_the_same_plan_and_writes_nothing(
    session_factory, artifact_store
) -> None:
    from tests.support.intervention import make_episode

    async with session_factory() as s:
        episode_id = await make_episode(s)
    activities = AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    )
    request = PinWorkflowPlanRequest(
        episode_id=episode_id, template_id="shorts_agentic", template_version="1"
    )
    first = await activities.pin_workflow_plan(request)
    second = await activities.pin_workflow_plan(request)  # 再起動の2回目

    assert first.existing is False and second.existing is True
    assert second.plan == first.plan
    async with session_factory() as s:
        rows = await s.scalar(
            select(func.count()).select_from(ProviderReservationRow)
        )  # pin は予約を作らない
        current = await ArtifactMetadataRepository(s).find_current_by_type(
            episode_id, ArtifactType.WORKFLOW_PLAN
        )
    assert rows == 0 and current is not None and current.version == 1
