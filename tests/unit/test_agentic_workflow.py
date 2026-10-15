"""AgenticEpisodeWorkflow / AgenticLearningWorkflow（ADR-0043 §6、INV-51・INV-55）。

time-skipping テストサーバ + SQLite + InMemoryArtifactStore。工程の子 workflow と
``run_agent_stage`` は同じ名前で登録した fake（非 sandbox）。pipeline 側の Activity は本物
（control gate・upload gate・pin・adopt）。各テストの理由: docs/testing/jpmas2-p1c-rationale.md
"""

from __future__ import annotations

import dataclasses
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from contracts.agentic import StrategyBrief, WorkflowPlan
from contracts.agentic_pipeline import (
    AGENT_SPEC_DRIFT_ERROR,
    AGENTIC_EPISODE_WORKFLOW,
    AGENTIC_LEARNING_WORKFLOW,
    AGENTS_TASK_QUEUE,
    RUN_AGENT_STAGE_ACTIVITY,
    AgenticEpisodeInput,
    AgenticEpisodeResult,
    AgenticLearningInput,
    AgenticLearningResult,
    AgenticOutcome,
    AgentStageOutcome,
    RunAgentStageRequest,
    RunAgentStageResult,
    agentic_episode_workflow_id,
    agentic_learning_workflow_id,
)
from contracts.agents import resolve_agent_spec
from contracts.intervention import CommandType
from contracts.pipeline import PipelineOptions
from contracts.states import ArtifactType, EpisodeStatus
from contracts.workflow_templates import TEMPLATES
from infrastructure.agents.episode_artifacts import write_episode_artifact
from infrastructure.db.models import EpisodeRow
from infrastructure.db.repositories import ArtifactMetadataRepository, EpisodeRepository
from tests.support.intervention import envelope, make_episode, submit
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.agentic_activities import AgenticPipelineActivities
from workers.pipeline.agentic_workflow import AgenticEpisodeWorkflow, AgenticLearningWorkflow
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

STAGE_STATUS = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}
BUCKET = "artifacts"
FIXTURES = Path(__file__).parent / "fixtures" / "pre_logging"


@dataclass
class World:
    #: 起動の順序（"agent:<stage>" / 子 workflow 名）
    events: list[str] = field(default_factory=list)
    agent_requests: list[RunAgentStageRequest] = field(default_factory=list)
    child_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    child_ids: dict[str, str] = field(default_factory=dict)
    #: stage_id -> 返す結果
    agent_results: dict[str, RunAgentStageResult] = field(default_factory=dict)
    #: stage_id -> 投げる例外（回数つき）
    agent_errors: dict[str, ApplicationError | RuntimeError] = field(default_factory=dict)
    #: 子 workflow 名 -> 返す status
    child_status: dict[str, str] = field(default_factory=dict)
    write_strategy_brief: bool = True


WORLD = World()


def _child(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    WORLD.events.append(name)
    WORLD.child_payloads[name] = payload
    WORLD.child_ids[name] = workflow.info().workflow_id
    return {
        "episode_id": payload["episode_id"],
        "status": WORLD.child_status.get(name, STAGE_STATUS[name]),
    }


@workflow.defn(name="ScriptWorkflow")
class FakeScript:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _child("ScriptWorkflow", payload)


@workflow.defn(name="StoryboardWorkflow")
class FakeStoryboard:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _child("StoryboardWorkflow", payload)


@workflow.defn(name="ProductionWorkflow")
class FakeProduction:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _child("ProductionWorkflow", payload)


@workflow.defn(name="RenderWorkflow")
class FakeRender:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _child("RenderWorkflow", payload)


@workflow.defn(name="UploadWorkflow")
class FakeUpload:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _child("UploadWorkflow", payload)


def _brief(episode_id: str) -> StrategyBrief:
    return StrategyBrief.model_validate(
        {
            "episode_id": episode_id,
            "audience_hypothesis": "US 20s like power-structure stories.",
            "audience_basis": "assumed",
            "candidates": [
                {
                    "id": "c1",
                    "title": "Why the Shogun hid in plain sight",
                    "subject": "tokugawa_ieyasu",
                    "angle": "reason",
                    "era": "edo",
                    "hook": "He ruled Japan without ever being king.",
                    "rationale": "Strong curiosity gap.",
                    "status": "proposed",
                }
            ],
            "recommended_candidate_id": "c1",
        }
    )


class FakeAgentWorker:
    """``run_agent_stage`` の fake。strategy だけは本物の成果物を書く（producer が読む）。"""

    def __init__(self, session_factory: Any, store: Any) -> None:
        self._session_factory = session_factory
        self._store = store

    @activity.defn(name=RUN_AGENT_STAGE_ACTIVITY)
    async def run_agent_stage(self, request: RunAgentStageRequest) -> RunAgentStageResult:
        stage_id = request.stage["stage_id"]
        WORLD.events.append(f"agent:{stage_id}")
        WORLD.agent_requests.append(request)
        error = WORLD.agent_errors.get(stage_id)
        if error is not None:
            raise error
        if stage_id in WORLD.agent_results:
            return WORLD.agent_results[stage_id]
        if stage_id == "strategy" and WORLD.write_strategy_brief:
            async with self._session_factory() as session:
                await write_episode_artifact(
                    session,
                    self._store,
                    bucket=BUCKET,
                    episode_id=request.episode_id,
                    model=_brief(request.episode_id),
                )
                await session.commit()
        return RunAgentStageResult(outcome=AgentStageOutcome.PRODUCED.value)


@dataclass
class Env:
    client: Client
    queue: str
    stage_queue: str
    session_factory: Any
    store: Any

    def options(self) -> PipelineOptions:
        q = self.stage_queue
        return PipelineOptions(
            script_workflow=("ScriptWorkflow", q),
            storyboard_workflow=("StoryboardWorkflow", q),
            production_workflow=("ProductionWorkflow", q),
            render_workflow=("RenderWorkflow", q),
            upload_workflow=("UploadWorkflow", q),
            pipeline_task_queue=self.queue,
        )

    async def run(
        self,
        episode_id: str,
        template: str = "shorts_agentic",
        version: str = "1",
        *,
        workflow_id: str | None = None,
    ) -> AgenticEpisodeResult:
        return await self.client.execute_workflow(
            AGENTIC_EPISODE_WORKFLOW[0],
            AgenticEpisodeInput(
                episode_id=episode_id,
                template_id=template,
                template_version=version,
                options=self.options(),
            ),
            id=workflow_id or agentic_episode_workflow_id(episode_id),
            task_queue=self.queue,
            result_type=AgenticEpisodeResult,
        )

    async def learn(self, episode_id: str) -> AgenticLearningResult:
        return await self.client.execute_workflow(
            AGENTIC_LEARNING_WORKFLOW[0],
            AgenticLearningInput(episode_id=episode_id),
            id=agentic_learning_workflow_id(episode_id) + uuid.uuid4().hex[:6],
            task_queue=self.queue,
            result_type=AgenticLearningResult,
        )


@pytest_asyncio.fixture
async def env(session_factory, artifact_store):
    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue = f"pipeline-{uuid.uuid4()}"
        stage_queue = f"stages-{uuid.uuid4()}"
        pipeline = PipelineActivities(
            session_factory=session_factory, paused_env=False, uploads_paused_env=False
        )
        agentic = AgenticPipelineActivities(
            session_factory=session_factory, store=artifact_store, bucket=BUCKET
        )
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[AgenticEpisodeWorkflow, AgenticLearningWorkflow],
            activities=[*pipeline.activities(), *agentic.activities()],
        )
        stages = Worker(
            wf_env.client,
            task_queue=stage_queue,
            workflows=[FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        agents = Worker(
            wf_env.client,
            task_queue=AGENTS_TASK_QUEUE,
            activities=[FakeAgentWorker(session_factory, artifact_store).run_agent_stage],
        )
        async with main, stages, agents:
            yield Env(wf_env.client, queue, stage_queue, session_factory, artifact_store)


async def _episode(session, session_factory, *, status: EpisodeStatus | None = None) -> str:
    """既定は planned。upload gate を通したいときは render_ready + final_video。"""
    episode_id = await make_episode(session)
    if status is not None:
        async with session_factory() as s:
            await s.execute(
                update(EpisodeRow)
                .where(EpisodeRow.id == uuid.UUID(episode_id))
                .values(status=status.value)
            )
            await ArtifactMetadataRepository(s).record(
                episode_id=episode_id,
                artifact_type=ArtifactType.FINAL_VIDEO,
                schema_version="1.0",
                bucket=BUCKET,
                object_key=f"episodes/{episode_id}/final.mp4",
                sha256="a" * 64,
                input_hash="h" * 64,
            )
            await s.commit()
    return episode_id


async def _ready_episode(session, session_factory) -> str:
    return await _episode(session, session_factory, status=EpisodeStatus.RENDER_READY)


FULL_ORDER = [
    "agent:strategy",
    "agent:creative",
    "ScriptWorkflow",
    "StoryboardWorkflow",
    "ProductionWorkflow",
    "RenderWorkflow",
    "UploadWorkflow",
    "agent:quality",
]


# ------------------------------------------------------------------------------ 順序


async def test_shorts_agentic_runs_the_stages_in_plan_order(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.COMPLETED.value
    assert WORLD.events == FULL_ORDER
    assert result.completed_stages == [
        "strategy",
        "producer",
        "creative",
        "script",
        "storyboard",
        "production",
        "render",
        "private_upload",
        "quality",
    ]
    # producer の採用が topic を書いた（TopicPlan 無しのまま）
    async with session_factory() as s:
        episode = await EpisodeRepository(s).get(episode_id)
    assert episode is not None
    assert episode.topic == "Why the Shogun hid in plain sight" and episode.topic_plan_id is None


async def test_existing_stages_use_the_same_ids_queues_and_payloads_as_the_pipeline(
    env: Env, session, session_factory
) -> None:
    from contracts.pipeline import PipelineStage
    from workers.pipeline.workflows import _stage_request

    episode_id = await _ready_episode(session, session_factory)
    await env.run(episode_id)
    expected = {
        "ScriptWorkflow": PipelineStage.SCRIPT,
        "StoryboardWorkflow": PipelineStage.STORYBOARD,
        "ProductionWorkflow": PipelineStage.PRODUCTION,
        "RenderWorkflow": PipelineStage.RENDER,
        "UploadWorkflow": PipelineStage.UPLOAD,
    }
    for name, stage in expected.items():
        (_n, _queue), child_id, payload = _stage_request(stage, episode_id, env.options())
        assert WORLD.child_ids[name] == child_id
        assert WORLD.child_payloads[name] == payload


async def test_agent_stage_requests_carry_the_pinned_spec_and_plan_hash(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    result = await env.run(episode_id)
    by_stage = {r.stage["stage_id"]: r for r in WORLD.agent_requests}
    assert set(by_stage) == {"strategy", "creative", "quality"}
    spec = resolve_agent_spec(by_stage["strategy"].stage["agent_role"], "1.0")
    assert by_stage["strategy"].stage["agent_spec_hash"] == spec.manifest_hash
    assert {r.plan_hash for r in WORLD.agent_requests} == {result.plan_hash}
    assert by_stage["strategy"].template_id == "shorts_agentic"


async def test_quality_runs_exactly_once_and_proposals_are_only_counted(
    env: Env, session, session_factory
) -> None:
    """INV-57 の workflow 側: 後ろ向きの辺が無く、QA の提案は数えるだけで自動適用しない。"""
    episode_id = await _ready_episode(session, session_factory)
    WORLD.agent_results["quality"] = RunAgentStageResult(
        outcome=AgentStageOutcome.PRODUCED.value, proposal_command_ids=["c-1", "c-2"]
    )
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.COMPLETED.value
    assert WORLD.events.count("agent:quality") == 1
    assert WORLD.events[-1] == "agent:quality"
    assert result.qa_repair_cycles == 2
    assert WORLD.events.count("ScriptWorkflow") == 1  # 修正のための再実行は無い


# ------------------------------------------------------------------------------ INV-51


async def test_the_pinned_plan_is_used_after_the_template_changes(
    env: Env, session, session_factory, monkeypatch
) -> None:
    """INV-51 runtime: 1回目の途中で止まった Episode は、Template が変わっても元の計画で続く。"""
    episode_id = await _ready_episode(session, session_factory)
    WORLD.agent_results["creative"] = RunAgentStageResult(
        outcome=AgentStageOutcome.BLOCKED.value, reason="held"
    )
    first = await env.run(episode_id)
    assert first.outcome == AgenticOutcome.STOPPED.value and first.plan_hash

    original = TEMPLATES[("shorts_agentic", "1")]
    changed = dataclasses.replace(original, stages=original.stages[:-1])  # quality を外した別の計画
    monkeypatch.setitem(TEMPLATES, ("shorts_agentic", "1"), changed)
    WORLD.agent_results.clear()
    WORLD.events.clear()
    second = await env.run(episode_id, workflow_id=f"rerun-{uuid.uuid4()}")
    assert second.plan_hash == first.plan_hash
    assert second.outcome == AgenticOutcome.COMPLETED.value
    assert (
        WORLD.events[-1] == "agent:quality"
    )  # 変更後の Template には無い stage が、元の計画で走る
    other = await _ready_episode(session, session_factory)
    WORLD.events.clear()
    third = await env.run(other)
    assert third.plan_hash != first.plan_hash and "agent:quality" not in WORLD.events


@pytest.mark.parametrize(
    ("template", "version", "code"),
    [
        ("daily_legacy", "1", "template_not_runnable"),
        ("shorts_reviewed", "1", "template_not_runnable"),
        ("shorts_agentic", "9", "unknown_template"),
        ("nope", "1", "unknown_template"),
    ],
)
async def test_unknown_unrunnable_and_legacy_templates_are_refused_before_any_stage(
    env: Env, session, session_factory, template: str, version: str, code: str
) -> None:
    episode_id = await _episode(session, session_factory)
    result = await env.run(episode_id, template, version)
    assert result.outcome == AgenticOutcome.STOPPED.value
    assert code in (result.reason or "")
    assert result.plan_hash is None and WORLD.events == []


async def test_a_drifted_agent_spec_stops_without_retrying(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    WORLD.agent_errors["strategy"] = ApplicationError(
        "hash differs", type=AGENT_SPEC_DRIFT_ERROR, non_retryable=True
    )
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value and result.stopped_stage == "strategy"
    assert AGENT_SPEC_DRIFT_ERROR in (result.reason or "")
    assert WORLD.events == ["agent:strategy"]


async def test_a_failing_agent_stage_is_retried_up_to_the_pinned_spec_limit(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    WORLD.agent_errors["strategy"] = RuntimeError("transient")
    result = await env.run(episode_id)
    limit = resolve_agent_spec(WORLD.agent_requests[0].stage["agent_role"], "1.0")
    assert WORLD.events.count("agent:strategy") == limit.retry_max_attempts
    assert result.outcome == AgenticOutcome.STOPPED.value and "agent stage failed" in (
        result.reason or ""
    )


# ------------------------------------------------------------------------------ 止まる・待つ


async def test_a_held_episode_parks_before_the_first_stage(
    env: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.PARKED.value
    assert result.stopped_stage == "strategy"
    assert WORLD.events == []


@pytest.mark.parametrize(
    "outcome", [AgentStageOutcome.BLOCKED.value, AgentStageOutcome.REJECTED_OUTPUT.value]
)
async def test_a_blocked_or_rejected_agent_stage_stops_with_a_recorded_reason(
    env: Env, session, session_factory, outcome: str
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    WORLD.agent_results["creative"] = RunAgentStageResult(outcome=outcome, reason="budget_exceeded")
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value and result.stopped_stage == "creative"
    assert outcome in (result.reason or "") and "budget_exceeded" in (result.reason or "")
    assert "ScriptWorkflow" not in WORLD.events  # 黙って飛ばさない
    assert result.completed_stages == ["strategy", "producer"]


async def test_the_producer_stops_the_workflow_when_there_is_nothing_to_adopt(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    WORLD.write_strategy_brief = False
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value and result.stopped_stage == "producer"
    assert "no_strategy_brief" in (result.reason or "")
    assert "agent:creative" not in WORLD.events


async def test_a_child_that_parks_elsewhere_stops_the_workflow(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    WORLD.child_status["StoryboardWorkflow"] = "needs_input"
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value and result.stopped_stage == "storyboard"
    assert "expected storyboard_ready" in (result.reason or "")
    assert WORLD.events[-1] == "StoryboardWorkflow"


async def test_the_upload_gate_is_checked_before_the_upload_and_refusal_stops(
    env: Env, session, session_factory
) -> None:
    episode_id = await _episode(session, session_factory)  # planned: render_ready ではない
    result = await env.run(episode_id)
    assert result.outcome == AgenticOutcome.STOPPED.value
    assert result.stopped_stage == "private_upload" and "upload skipped" in (result.reason or "")
    assert "UploadWorkflow" not in WORLD.events and "agent:quality" not in WORLD.events


# ------------------------------------------------------------------------------ Learning


async def test_learning_runs_the_pinned_post_publish_stage_and_returns_the_outcome_as_is(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    await env.run(episode_id)
    plan = WorkflowPlan.model_validate(await _pinned(session_factory, env.store, episode_id))
    WORLD.events.clear()
    WORLD.agent_requests.clear()
    WORLD.agent_results["lesson"] = RunAgentStageResult(
        outcome=AgentStageOutcome.AWAITING_PUBLIC_METRICS.value, reason="private video"
    )
    result = await env.learn(episode_id)
    assert result.outcome == AgentStageOutcome.AWAITING_PUBLIC_METRICS.value
    assert result.lesson_revision_id is None and result.reason == "private video"
    (request,) = WORLD.agent_requests
    assert request.stage["stage_id"] == "lesson" and request.plan_hash == plan.plan_hash
    assert WORLD.events == ["agent:lesson"]  # 子 workflow・producer・pin は走らない


async def _pinned(session_factory: Any, store: Any, episode_id: str) -> dict[str, Any]:
    from infrastructure.agents.episode_artifacts import read_current_artifact

    async with session_factory() as s:
        current = await read_current_artifact(s, store, episode_id, ArtifactType.WORKFLOW_PLAN)
    assert current is not None
    return current[1].model_dump(mode="json")


async def test_learning_returns_the_lesson_revision_when_one_was_produced(
    env: Env, session, session_factory
) -> None:
    episode_id = await _ready_episode(session, session_factory)
    await env.run(episode_id)
    WORLD.agent_results["lesson"] = RunAgentStageResult(
        outcome=AgentStageOutcome.PRODUCED.value, artifact_revision_id="rev-1"
    )
    result = await env.learn(episode_id)
    assert result.outcome == AgentStageOutcome.PRODUCED.value
    assert result.lesson_revision_id == "rev-1"


async def test_learning_without_a_pinned_plan_is_blocked_and_pins_nothing(
    env: Env, session, session_factory
) -> None:
    episode_id = await _episode(session, session_factory)
    result = await env.learn(episode_id)
    assert result.outcome == AgentStageOutcome.BLOCKED.value
    assert "no_pinned_plan" in (result.reason or "")
    assert WORLD.events == []
    async with session_factory() as s:
        assert (
            await ArtifactMetadataRepository(s).find_current_by_type(
                episode_id, ArtifactType.WORKFLOW_PLAN
            )
            is None
        )


# ------------------------------------------------------------------------------ 日次は不変


@pytest.mark.parametrize(
    "name",
    ["pipeline_completed", "pipeline_resume_mid", "pipeline_upload_gate_refused", "daily_started"],
)
async def test_legacy_histories_still_replay_with_the_agentic_workflows_registered(
    name: str,
) -> None:
    """INV-55: agentic の workflow を同じ worker に載せても日次の履歴は replay できる。"""
    history = WorkflowHistory.from_json(name, (FIXTURES / f"{name}.json").read_text("utf-8"))
    await Replayer(
        workflows=[
            DailyEpisodeWorkflow,
            EpisodePipelineWorkflow,
            AgenticEpisodeWorkflow,
            AgenticLearningWorkflow,
        ]
    ).replay_workflow(history)


async def test_a_history_produced_by_the_legacy_workflow_replays(
    session_factory, artifact_store
) -> None:
    """今のコードで走らせた legacy EpisodePipelineWorkflow の履歴を replay する（記録 -> 再生）。"""
    from contracts.pipeline import EpisodePipelineInput, pipeline_workflow_id

    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue, stage_queue = f"p-{uuid.uuid4()}", f"s-{uuid.uuid4()}"
        pipeline = PipelineActivities(
            session_factory=session_factory, paused_env=False, uploads_paused_env=False
        )
        async with (
            Worker(
                wf_env.client,
                task_queue=queue,
                workflows=[EpisodePipelineWorkflow],
                activities=pipeline.activities(),
            ),
            Worker(
                wf_env.client,
                task_queue=stage_queue,
                workflows=[FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload],
                workflow_runner=UnsandboxedWorkflowRunner(),
            ),
        ):
            options = Env(
                wf_env.client, queue, stage_queue, session_factory, artifact_store
            ).options()
            async with session_factory() as s:
                episode_id = await _ready_episode(s, session_factory)
            handle = await wf_env.client.start_workflow(
                "EpisodePipelineWorkflow",
                EpisodePipelineInput(episode_id=episode_id, options=options),
                id=pipeline_workflow_id(episode_id),
                task_queue=queue,
            )
            await handle.result()
            history = await handle.fetch_history()
    await Replayer(
        workflows=[EpisodePipelineWorkflow, AgenticEpisodeWorkflow, AgenticLearningWorkflow]
    ).replay_workflow(history)


def test_the_legacy_workflow_module_does_not_mention_the_agentic_workflow() -> None:
    source = (Path(__file__).resolve().parents[2] / "workers/pipeline/workflows.py").read_text()
    for needle in ("agentic", "Agentic", "workflow_plan"):
        assert needle not in source


async def test_a_parked_run_can_be_started_again_with_the_same_id_and_the_same_plan(
    env: Env, session, session_factory, artifact_store
) -> None:
    """hold で parked → resume で同じ id を再起動。同じ計画を歩き直し、冪等キーは変わらない。"""
    episode_id = await _ready_episode(session, session_factory)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    parked = await env.run(episode_id)
    assert parked.outcome == AgenticOutcome.PARKED.value and WORLD.events == []
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.RESUME))

    resumed = await env.run(episode_id)  # 閉じた id の再利用（既定の ALLOW_DUPLICATE）

    assert resumed.outcome == AgenticOutcome.COMPLETED.value
    assert resumed.plan_hash == parked.plan_hash
    assert WORLD.events == FULL_ORDER
    assert {r.idempotency_scope for r in WORLD.agent_requests} == {
        None
    }  # Learning の scope は無関係
    assert {r.plan_hash for r in WORLD.agent_requests} == {parked.plan_hash}
    async with session_factory() as s:
        current = await ArtifactMetadataRepository(s).find_current_by_type(
            episode_id, ArtifactType.WORKFLOW_PLAN
        )
        assert current is not None and current.version == 1  # 計画は再 pin されない
