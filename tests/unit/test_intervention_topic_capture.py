"""Topic Planner（producer）の実行記録と ``choose_topic``（JP-MAS-001 §7、ADR-0041）。

Planner は Episode より前に走るので mode では切り替えられない。設定
``TOPIC_PLANNER_CAPTURE_ENABLED``（既定 OFF）が ON のときだけ記録する。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from contracts.intervention import AgentRole, RationaleKind
from contracts.topic_planning import SelectAndSaveRequest
from infrastructure.config import Settings
from infrastructure.db.models import AgentRunRow, DecisionRecordRow
from infrastructure.db.repositories import TopicPlanRepository
from infrastructure.intervention.capture import AGENT_RUNS_PREFIX, AgentRunCaptureError
from infrastructure.intervention.store import InterventionStore
from infrastructure.storage.memory_store import InMemoryArtifactStore
from tests.support.fakes import FakeStoryGenerator
from tests.support.research_opt_in import CANDIDATES_OUTPUT, planner_request
from workers.planning import run_worker
from workers.planning.topic_activities import TopicPlannerActivities


class _Store(InMemoryArtifactStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = False

    async def put_json(self, key: str, payload: Any):  # type: ignore[override]
        if self.fail:
            raise OSError("minio down")
        return await super().put_json(key, payload)

    def object_keys(self) -> list[str]:
        return sorted(self._objects)


def _activities(session_factory, generator, capture_store=None) -> TopicPlannerActivities:
    return TopicPlannerActivities(
        session_factory=session_factory,
        generator=generator,
        analytics=None,
        analytics_provider_id="youtube_analytics",
        clock=lambda: datetime(2026, 10, 7, tzinfo=UTC),
        timeout_seconds=60,
        capture_store=capture_store,
    )


async def _plan(activities: TopicPlannerActivities):
    request = planner_request()
    generated = await activities.generate_candidates(request)
    return await activities.select_and_save(
        SelectAndSaveRequest(
            context=request.context,
            candidates=generated.candidates,
            prompt_version=generated.prompt_version,
            round=request.round,
        )
    )


async def _count(session_factory, model) -> int:
    async with session_factory() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_capture_is_off_by_default(session_factory) -> None:
    assert Settings(_env_file=None).topic_planner_capture_enabled is False  # pyright: ignore[reportCallIssue]
    saved = await _plan(_activities(session_factory, FakeStoryGenerator(output=CANDIDATES_OUTPUT)))
    assert saved.topic_plan_id is not None
    assert await _count(session_factory, AgentRunRow) == 0
    assert await _count(session_factory, DecisionRecordRow) == 0


def test_the_worker_passes_a_capture_store_only_when_enabled(monkeypatch) -> None:
    store = InMemoryArtifactStore()
    monkeypatch.delenv("TOPIC_PLANNER_CAPTURE_ENABLED", raising=False)
    off = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert run_worker.topic_capture_store(off, store) is None
    monkeypatch.setenv("TOPIC_PLANNER_CAPTURE_ENABLED", "true")
    on = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]
    assert run_worker.topic_capture_store(on, store) is store


async def test_enabled_capture_records_the_run_and_the_choice_with_the_plan(
    session_factory,
) -> None:
    store = _Store()
    generator = FakeStoryGenerator(output=CANDIDATES_OUTPUT)
    saved = await _plan(_activities(session_factory, generator, store))
    assert saved.topic_plan_id is not None

    async with session_factory() as session:
        intervention = InterventionStore(session)
        (run,) = await intervention.list_agent_runs(topic_plan_id=saved.topic_plan_id)
        (decision,) = await intervention.list_decisions(topic_plan_id=saved.topic_plan_id)
        candidates = await TopicPlanRepository(session).list_candidates(saved.topic_plan_id)
        plan = await TopicPlanRepository(session).get(saved.topic_plan_id)
    assert plan is not None
    assert run.role is AgentRole.PRODUCER and run.step == "choose_topic"
    assert run.episode_id is None and run.topic_plan_id == saved.topic_plan_id
    assert run.snapshot_object_key.startswith(
        f"{AGENT_RUNS_PREFIX}/{saved.topic_plan_id}/{run.agent_run_id}/"
    )
    assert run.prompt_version == plan.prompt_version
    assert run.token_usage is None
    snapshot = json.loads(await store.get_bytes(run.snapshot_object_key))
    assert snapshot["prompt"] == generator.requests[0].prompt
    assert snapshot["prompt_source"] == "captured"
    assert snapshot["response"]["text"] == CANDIDATES_OUTPUT
    assert snapshot["input_refs"]["round"] == 2
    assert decision.agent_run_id == run.agent_run_id
    assert decision.payload["input_snapshot_ref"] == run.snapshot_object_key
    assert decision.payload["selected_candidate"] == plan.selected_candidate_id
    assert [c["id"] for c in decision.payload["candidates"]] == [c.id for c in candidates]
    assert decision.payload["rationale_kind"] == RationaleKind.REPORTED.value


async def test_a_staging_failure_fails_candidate_generation(session_factory) -> None:
    store = _Store()
    store.fail = True
    with pytest.raises(AgentRunCaptureError):
        await _activities(
            session_factory, FakeStoryGenerator(output=CANDIDATES_OUTPUT), store
        ).generate_candidates(planner_request())


async def test_a_rerun_of_select_and_save_does_not_duplicate_records(session_factory) -> None:
    store = _Store()
    activities = _activities(session_factory, FakeStoryGenerator(output=CANDIDATES_OUTPUT), store)
    request = planner_request()
    generated = await activities.generate_candidates(request)
    save = SelectAndSaveRequest(
        context=request.context,
        candidates=generated.candidates,
        prompt_version=generated.prompt_version,
        round=request.round,
    )
    first = await activities.select_and_save(save)
    again = await activities.select_and_save(save)
    assert again.reused is True and again.topic_plan_id == first.topic_plan_id
    assert await _count(session_factory, AgentRunRow) == 1
    assert await _count(session_factory, DecisionRecordRow) == 1
