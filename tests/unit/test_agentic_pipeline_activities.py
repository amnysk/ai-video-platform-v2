"""pin_workflow_plan / adopt_strategy_candidate / load_workflow_plan（ADR-0043 §6、INV-51）。

SQLite + InMemoryArtifactStore。各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from sqlalchemy import func, select
from temporalio.exceptions import ApplicationError

from contracts.agentic import StrategyBrief, WorkflowPlan
from contracts.agentic_pipeline import (
    AdoptStrategyCandidateRequest,
    LoadWorkflowPlanRequest,
    PinWorkflowPlanRequest,
)
from contracts.intervention import ActorType, InterventionEvent, RationaleKind
from contracts.states import ArtifactType
from contracts.workflow_templates import TEMPLATES
from infrastructure.agents.episode_artifacts import write_episode_artifact
from infrastructure.db.models import TopicPlanRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from infrastructure.intervention.store import InterventionStore
from workers.pipeline.agentic_activities import AgenticPipelineActivities

BUCKET = "artifacts"
PIN = PinWorkflowPlanRequest(episode_id="", template_id="shorts_agentic", template_version="1")


def _acts(session_factory: Any, artifact_store: Any) -> AgenticPipelineActivities:
    return AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket=BUCKET
    )


async def _episode(session_factory: Any, topic: str = "draft theme") -> str:
    async with session_factory() as s:
        ep = await EpisodeRepository(s).create(topic=topic)
        await s.commit()
        return ep.id


def _candidate(id_: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": id_,
        "title": f"Why the Shogun hid in plain sight {id_}",
        "subject": "tokugawa_ieyasu",
        "angle": "reason",
        "era": "edo",
        "hook": "He ruled Japan without ever being king.",
        "rationale": "Strong curiosity gap.",
        "status": "proposed",
    }
    return {**base, **over}


async def _store_brief(session_factory: Any, artifact_store: Any, episode_id: str) -> None:
    brief = StrategyBrief.model_validate(
        {
            "episode_id": episode_id,
            "audience_hypothesis": "US 20s like power-structure stories.",
            "audience_basis": "assumed",
            "candidates": [
                _candidate("c1"),
                _candidate("c2", status="rejected", rejected_reason="too similar to last week"),
                _candidate("c3"),
            ],
            "recommended_candidate_id": "c3",
        }
    )
    async with session_factory() as s:
        await write_episode_artifact(
            s, artifact_store, bucket=BUCKET, episode_id=episode_id, model=brief
        )
        await s.commit()


# ------------------------------------------------------------------------------ pin


@pytest.mark.asyncio
async def test_pin_compiles_stores_and_records_the_plan(session_factory, artifact_store) -> None:
    ep = await _episode(session_factory)
    acts = _acts(session_factory, artifact_store)
    result = await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=ep))
    plan = WorkflowPlan.model_validate(result.plan)
    assert result.existing is False
    assert plan.template_id == "shorts_agentic" and plan.execution == "agentic"
    async with session_factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            ep, ArtifactType.WORKFLOW_PLAN
        )
        assert meta is not None and meta.schema_version == "1.0"
        events = await InterventionStore(s).list_events(ep)
    assert InterventionEvent.DECISION_RECORDED.value in [e.event_name for e in events]


@pytest.mark.asyncio
async def test_pin_reports_the_retry_limit_of_each_pinned_agent_spec(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory)
    result = await _acts(session_factory, artifact_store).pin_workflow_plan(
        dataclasses.replace(PIN, episode_id=ep)
    )
    assert set(result.retry_max_attempts) == {"strategy", "creative", "quality", "lesson"}
    assert all(v >= 1 for v in result.retry_max_attempts.values())


@pytest.mark.asyncio
async def test_a_pinned_plan_is_returned_unchanged_after_the_template_changes(
    session_factory, artifact_store, monkeypatch
) -> None:
    """INV-51 runtime: 実行開始後に TEMPLATES が変わっても、その Episode の計画は変わらない。"""
    ep = await _episode(session_factory)
    acts = _acts(session_factory, artifact_store)
    first = await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=ep))

    original = TEMPLATES[("shorts_agentic", "1")]
    monkeypatch.setitem(
        TEMPLATES, ("shorts_agentic", "1"), dataclasses.replace(original, max_qa_repair_cycles=1)
    )
    again = await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=ep))
    assert again.existing is True
    assert again.plan == first.plan
    # 別の Episode は変更後の Template を pin する（変更が効いていることの確認）
    other = await _episode(session_factory)
    fresh = await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=other))
    assert fresh.plan["max_qa_repair_cycles"] == 1
    assert fresh.plan["plan_hash"] != first.plan["plan_hash"]


@pytest.mark.asyncio
async def test_pin_is_idempotent_and_writes_one_artifact_and_one_decision(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory)
    acts = _acts(session_factory, artifact_store)
    for _ in range(3):
        await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=ep))
    async with session_factory() as s:
        artifacts = [
            a
            for a in await ArtifactMetadataRepository(s).list_for_episode(ep)
            if a.artifact_type is ArtifactType.WORKFLOW_PLAN
        ]
        decisions = await InterventionStore(s).list_decisions(episode_id=ep)
    assert len(artifacts) == 1
    assert [d.decision_type for d in decisions] == ["pin_workflow_plan"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("template", "version", "code"),
    [
        ("daily_legacy", "1", "template_not_runnable"),
        ("shorts_reviewed", "1", "template_not_runnable"),
        ("shorts_agentic", "9", "unknown_template"),
        ("nope", "1", "unknown_template"),
    ],
)
async def test_pin_refuses_legacy_unrunnable_and_unknown_templates(
    session_factory, artifact_store, template: str, version: str, code: str
) -> None:
    ep = await _episode(session_factory)
    with pytest.raises(ApplicationError) as err:
        await _acts(session_factory, artifact_store).pin_workflow_plan(
            PinWorkflowPlanRequest(episode_id=ep, template_id=template, template_version=version)
        )
    assert err.value.non_retryable is True
    assert err.value.type == code
    async with session_factory() as s:
        assert await ArtifactMetadataRepository(s).list_for_episode(ep) == []


@pytest.mark.asyncio
async def test_pin_refuses_a_missing_episode(session_factory, artifact_store) -> None:
    with pytest.raises(ApplicationError) as err:
        await _acts(session_factory, artifact_store).pin_workflow_plan(
            dataclasses.replace(PIN, episode_id="00000000-0000-0000-0000-000000000001")
        )
    assert err.value.non_retryable is True and err.value.type == "episode_not_found"


@pytest.mark.asyncio
async def test_load_returns_the_pinned_plan_or_refuses(session_factory, artifact_store) -> None:
    ep = await _episode(session_factory)
    acts = _acts(session_factory, artifact_store)
    with pytest.raises(ApplicationError) as err:
        await acts.load_workflow_plan(LoadWorkflowPlanRequest(episode_id=ep))
    assert err.value.non_retryable is True and err.value.type == "no_pinned_plan"
    pinned = await acts.pin_workflow_plan(dataclasses.replace(PIN, episode_id=ep))
    loaded = await acts.load_workflow_plan(LoadWorkflowPlanRequest(episode_id=ep))
    assert loaded.plan == pinned.plan and loaded.existing is True


# ---------------------------------------------------------------------------- adopt


def _adopt(ep: str) -> AdoptStrategyCandidateRequest:
    return AdoptStrategyCandidateRequest(episode_id=ep, stage_id="producer", plan_hash="a" * 64)


@pytest.mark.asyncio
async def test_adopt_chooses_the_recommended_candidate_by_rule(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory)
    await _store_brief(session_factory, artifact_store, ep)
    result = await _acts(session_factory, artifact_store).adopt_strategy_candidate(_adopt(ep))
    assert result.adopted is True and result.candidate_id == "c3" and result.decision_id
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(ep)
        decisions = await InterventionStore(s).list_decisions(episode_id=ep)
    assert episode is not None
    assert episode.topic == "Why the Shogun hid in plain sight c3"
    assert episode.topic_plan_id is None
    (decision,) = decisions
    assert decision.decision_type == "choose_topic"
    assert (decision.actor.type, decision.actor.id) == (ActorType.AGENT, "producer")
    assert decision.decision_id == result.decision_id
    payload = decision.payload
    assert payload["rationale_kind"] == RationaleKind.RULE.value
    assert payload["selected_candidate"] == "c3"
    assert [c["id"] for c in payload["candidates"]] == ["c1", "c2", "c3"]
    rejected = {c["id"]: c["rejected_reason"] for c in payload["candidates"]}
    assert rejected["c2"] == "too similar to last week"
    assert rejected["c3"] is None and rejected["c1"]  # 採用されなかった理由は補完せず規則の事実だけ


@pytest.mark.asyncio
async def test_adopt_is_idempotent_and_never_creates_a_topic_plan(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory)
    await _store_brief(session_factory, artifact_store, ep)
    acts = _acts(session_factory, artifact_store)
    first = await acts.adopt_strategy_candidate(_adopt(ep))
    second = await acts.adopt_strategy_candidate(_adopt(ep))
    assert first == second
    async with session_factory() as s:
        decisions = await InterventionStore(s).list_decisions(episode_id=ep)
        plans = await s.scalar(select(func.count()).select_from(TopicPlanRow))
    assert len(decisions) == 1
    assert plans == 0


@pytest.mark.asyncio
async def test_adopt_without_a_strategy_brief_stops_and_changes_nothing(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory, topic="keep me")
    result = await _acts(session_factory, artifact_store).adopt_strategy_candidate(_adopt(ep))
    assert result.adopted is False and result.reason == "no_strategy_brief"
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(ep)
        assert await InterventionStore(s).list_decisions(episode_id=ep) == []
    assert episode is not None and episode.topic == "keep me"


@pytest.mark.asyncio
async def test_adopt_refuses_a_brief_written_for_another_episode(
    session_factory, artifact_store
) -> None:
    ep = await _episode(session_factory)
    other = await _episode(session_factory)
    await _store_brief(session_factory, artifact_store, other)
    # 他 Episode の brief を ep の現行として置く（取り違え）
    async with session_factory() as s:
        meta = await ArtifactMetadataRepository(s).find_current_by_type(
            other, ArtifactType.STRATEGY_BRIEF
        )
        assert meta is not None
        await ArtifactMetadataRepository(s).record(
            episode_id=ep,
            artifact_type=ArtifactType.STRATEGY_BRIEF,
            schema_version="1.0",
            bucket=meta.bucket,
            object_key=meta.object_key,
            sha256=meta.sha256,
        )
        await s.commit()
    result = await _acts(session_factory, artifact_store).adopt_strategy_candidate(_adopt(ep))
    assert result.adopted is False and result.reason == "strategy_brief_episode_mismatch"
