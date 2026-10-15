"""介入 command の Temporal 接続と pipeline の control gate（ADR-0041 §3・§5）。

time-skipping テストサーバ + SQLite（StaticPool）+ InMemoryArtifactStore。工程の子 workflow は
同じ名前で登録した fake（非 sandbox）。Activity は本物（``InterventionActivities`` /
``PipelineActivities``）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from contracts.intervention import (
    CONTROL_GATE_PATCH_ID,
    PIPELINE_CONTROL_GATE_ACTIVITY,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    ReviewPoint,
    intervention_command_workflow_id,
)
from contracts.pipeline import (
    ControlGateRequest,
    EpisodePipelineInput,
    PipelineOptions,
    PipelineOutcome,
    pipeline_workflow_id,
)
from contracts.states import EpisodeStatus
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.store import InterventionStore
from tests.support.intervention import (
    NOW,
    apply,
    envelope,
    make_episode,
    receive,
    submit,
)
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import (
    InterventionCommandWorkflow,
    InterventionRedeliveryWorkflow,
)
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.workflows import EpisodePipelineWorkflow

STAGE_STATUS = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}


@dataclass
class World:
    stage_calls: list[str] = field(default_factory=list)
    pipeline_inputs: list[dict[str, Any]] = field(default_factory=list)


WORLD = World()


def _stage(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    WORLD.stage_calls.append(name)
    return {"episode_id": payload["episode_id"], "status": STAGE_STATUS[name]}


@workflow.defn(name="ScriptWorkflow")
class FakeScript:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _stage("ScriptWorkflow", payload)


@workflow.defn(name="StoryboardWorkflow")
class FakeStoryboard:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _stage("StoryboardWorkflow", payload)


@workflow.defn(name="ProductionWorkflow")
class FakeProduction:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _stage("ProductionWorkflow", payload)


@workflow.defn(name="RenderWorkflow")
class FakeRender:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _stage("RenderWorkflow", payload)


@workflow.defn(name="UploadWorkflow")
class FakeUpload:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _stage("UploadWorkflow", payload)


@workflow.defn(name="EpisodePipelineWorkflow")
class HeldPipeline:
    """command の検査用: start されたら release の signal まで走り続ける pipeline。"""

    def __init__(self) -> None:
        self.released = False

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        WORLD.pipeline_inputs.append(payload)
        await workflow.wait_condition(lambda: self.released)
        return {"episode_id": payload["episode_id"], "outcome": "completed", "status": ""}

    @workflow.signal(name="release")
    def release(self) -> None:
        self.released = True


@dataclass
class Env:
    client: Client
    queue: str
    stage_queue: str
    held_queue: str


def _options(stage_queue: str, pipeline_queue: str) -> PipelineOptions:
    return PipelineOptions(
        script_workflow=("ScriptWorkflow", stage_queue),
        storyboard_workflow=("StoryboardWorkflow", stage_queue),
        production_workflow=("ProductionWorkflow", stage_queue),
        render_workflow=("RenderWorkflow", stage_queue),
        upload_workflow=("UploadWorkflow", stage_queue),
        pipeline_task_queue=pipeline_queue,
    )


@pytest_asyncio.fixture
async def real_pipeline(session_factory, artifact_store):
    """本物の EpisodePipelineWorkflow と control gate（command workflow が本物を start）。"""
    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue = f"pipeline-{uuid.uuid4()}"
        stage_queue = f"stages-{uuid.uuid4()}"
        intervention = InterventionActivities(
            session_factory=session_factory,
            store=artifact_store,
            budget_policy=BudgetPolicy(),
            pipeline_options=_options(stage_queue, queue),
            command_task_queue=queue,
            clock=lambda: NOW,
        )
        pipeline = PipelineActivities(
            session_factory=session_factory, paused_env=False, uploads_paused_env=False
        )
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[EpisodePipelineWorkflow, InterventionCommandWorkflow],
            activities=[*pipeline.activities(), *intervention.activities()],
        )
        stages = Worker(
            wf_env.client,
            task_queue=stage_queue,
            workflows=[FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, stages:
            yield Env(wf_env.client, queue, stage_queue, "")


@pytest_asyncio.fixture
async def held_pipeline(session_factory, artifact_store):
    """command workflow の検査用: pipeline は start の回数を数える fake。"""
    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue = f"intervention-{uuid.uuid4()}"
        held_queue = f"held-{uuid.uuid4()}"
        intervention = InterventionActivities(
            session_factory=session_factory,
            store=artifact_store,
            budget_policy=BudgetPolicy(),
            pipeline_options=_options("unused", held_queue),
            command_task_queue=queue,
            clock=lambda: NOW,
        )
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[InterventionCommandWorkflow, InterventionRedeliveryWorkflow],
            activities=intervention.activities(),
        )
        held = Worker(
            wf_env.client,
            task_queue=held_queue,
            workflows=[HeldPipeline],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, held:
            # 時間を飛ばすと、走り続ける fake pipeline が待っている間に timeout してしまう
            with wf_env.auto_time_skipping_disabled():
                yield Env(wf_env.client, queue, "", held_queue)


async def _run_command(env: Env, command_id: str) -> dict[str, Any]:
    return await env.client.execute_workflow(
        "InterventionCommandWorkflow",
        {"command_id": command_id},  # apps/api/intervention_backend.TemporalCommandStarter と同じ形
        id=intervention_command_workflow_id(command_id),
        task_queue=env.queue,
        result_type=dict,
    )


async def _events(session_factory, episode_id: str) -> list[InterventionEvent]:
    async with session_factory() as s:
        return [e.event_name for e in await InterventionStore(s).list_events(episode_id)]


# --------------------------------------------------------------------------- command workflow


async def test_resume_applies_once_starts_the_pipeline_once_and_reports_duplicates(
    held_pipeline: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    resume = envelope(CommandType.RESUME)
    await receive(session_factory, episode_id, resume)

    first = await _run_command(held_pipeline, resume.command_id)
    assert first["status"] == CommandStatus.APPLIED.value
    assert first["pipeline"]["outcome"] == "started"
    assert first["pipeline"]["workflow_id"] == pipeline_workflow_id(episode_id)
    assert len(WORLD.pipeline_inputs) == 1
    started = WORLD.pipeline_inputs[0]
    assert started["episode_id"] == episode_id and started["control_gate"] is False
    assert started["options"]["pipeline_task_queue"] == held_pipeline.held_queue

    # API の再送で同じ command workflow が再び走っても、適用も pipeline の start も増えない
    again = await held_pipeline.client.execute_workflow(
        "InterventionCommandWorkflow",
        {"command_id": resume.command_id},
        id=f"{intervention_command_workflow_id(resume.command_id)}-resend",
        task_queue=held_pipeline.queue,
        result_type=dict,
    )
    assert again["replayed"] is True and again["pipeline"] == first["pipeline"]
    assert len(WORLD.pipeline_inputs) == 1

    # pipeline が走っている間の別の resume は already_running として記録される
    second = envelope(CommandType.RESUME)
    await receive(session_factory, episode_id, second)
    duplicate = await _run_command(held_pipeline, second.command_id)
    assert duplicate["status"] == CommandStatus.APPLIED.value
    assert duplicate["pipeline"]["outcome"] == "already_running"
    assert len(WORLD.pipeline_inputs) == 1
    await held_pipeline.client.get_workflow_handle(pipeline_workflow_id(episode_id)).signal(
        "release"
    )


async def test_a_crash_after_the_db_apply_recovers_to_the_same_single_change(
    held_pipeline: Env, session, session_factory, artifact_store
) -> None:
    """INV-42: apply の commit 後に落ちた → 再配送は保存済み result で pipeline だけ始める。"""
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    resume = envelope(CommandType.RESUME)
    await receive(session_factory, episode_id, resume)
    applied = await apply(session_factory, artifact_store, resume.command_id)  # ここで「落ちた」
    async with session_factory() as s:
        revision = (await InterventionStore(s).get_control(episode_id)).control_revision

    recovered = await _run_command(held_pipeline, resume.command_id)
    assert recovered["replayed"] is True
    assert recovered["pipeline"]["outcome"] == "started"
    async with session_factory() as s:
        control = await InterventionStore(s).get_control(episode_id)
        command = await InterventionStore(s).get_command(resume.command_id)
    assert control.control_revision == revision
    assert command is not None and command.result is not None
    assert command.result["start_stage"] == applied.result["start_stage"]
    assert (await _events(session_factory, episode_id)).count(
        InterventionEvent.RESUME_REQUESTED
    ) == 1
    assert len(WORLD.pipeline_inputs) == 1
    await held_pipeline.client.get_workflow_handle(pipeline_workflow_id(episode_id)).signal(
        "release"
    )


async def test_redelivery_starts_undelivered_commands_and_marks_them_dispatched(
    held_pipeline: Env, session, session_factory
) -> None:
    episode_id = await make_episode(session)
    hold = envelope(CommandType.REQUEST_HOLD)
    await receive(session_factory, episode_id, hold)  # API は commit 後の start に失敗した
    result = await held_pipeline.client.execute_workflow(
        "InterventionRedeliveryWorkflow",
        {},
        id=f"redelivery-{uuid.uuid4()}",
        task_queue=held_pipeline.queue,
        result_type=dict,
    )
    assert result["started"] == [hold.command_id]
    handle = held_pipeline.client.get_workflow_handle(
        intervention_command_workflow_id(hold.command_id)
    )
    outcome = await handle.result()
    assert outcome["status"] == CommandStatus.APPLIED.value
    async with session_factory() as s:
        command = await InterventionStore(s).get_command(hold.command_id)
        control = await InterventionStore(s).get_control(episode_id)
    assert command is not None and command.dispatched_at is not None
    assert control.control_state is ControlState.HELD


# --------------------------------------------------------------------------- control gate


async def _pipeline(env: Env, episode_id: str, *, start_stage: str, control_gate: bool):
    handle = await env.client.start_workflow(
        "EpisodePipelineWorkflow",
        EpisodePipelineInput(
            episode_id=episode_id,
            options=_options(env.stage_queue, env.queue),
            start_stage=start_stage,
            control_gate=control_gate,
        ),
        id=pipeline_workflow_id(episode_id),
        task_queue=env.queue,
    )
    result = await handle.result()
    return result, await handle.fetch_history()


def _activity_names(history: WorkflowHistory) -> list[str]:
    return [
        e.activity_task_scheduled_event_attributes.activity_type.name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]


def _markers(history: WorkflowHistory) -> list[str]:
    return [
        e.marker_recorded_event_attributes.marker_name
        for e in history.events
        if e.event_type == EventType.EVENT_TYPE_MARKER_RECORDED
    ]


def _patch_ids(history: WorkflowHistory) -> list[str]:
    """``workflow.patched`` が残す marker（core_patch）の patch id。"""
    ids: list[str] = []
    for e in history.events:
        if e.event_type != EventType.EVENT_TYPE_MARKER_RECORDED:
            continue
        details = e.marker_recorded_event_attributes.details
        if "patch-data" in details:
            ids.append(json.loads(details["patch-data"].payloads[0].data)["id"])
    return ids


async def test_a_control_gate_run_parks_at_a_review_point_and_resumes_after_release(
    real_pipeline: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session)
    await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.SET_MODE, {"mode": "co_production"}),
    )
    parked, history = await _pipeline(
        real_pipeline, episode_id, start_stage="script", control_gate=True
    )
    assert parked["outcome"] == PipelineOutcome.PARKED.value
    assert parked["stopped_stage"] == "storyboard"
    assert WORLD.stage_calls == ["ScriptWorkflow"]
    assert _activity_names(history).count(PIPELINE_CONTROL_GATE_ACTIVITY) == 2
    async with session_factory() as s:
        control = await InterventionStore(s).get_control(episode_id)
    assert control.control_state is ControlState.WAITING_REVIEW
    assert control.waiting_point is ReviewPoint.BEFORE_STORYBOARD
    assert control.waiting_manifest_id == control.active_manifest_id
    assert (await _events(session_factory, episode_id)).count(
        InterventionEvent.CHECKPOINT_WAITING
    ) == 1

    # 古い manifest への承認は流用されない／現在の manifest への承認で解放 → storyboard から再開
    release = envelope(
        CommandType.RELEASE_CHECKPOINT,
        {"point": "before_storyboard", "manifest_id": control.active_manifest_id},
    )
    await receive(session_factory, episode_id, release)
    released = await _run_command(real_pipeline, release.command_id)
    assert released["pipeline"]["outcome"] == "started"
    resumed = await real_pipeline.client.get_workflow_handle(
        pipeline_workflow_id(episode_id)
    ).result()
    assert resumed["outcome"] == PipelineOutcome.PARKED.value
    assert resumed["stopped_stage"] == "production"  # 次の待機地点（有料素材生成前）
    assert WORLD.stage_calls == ["ScriptWorkflow", "StoryboardWorkflow"]
    resumed_history = await real_pipeline.client.get_workflow_handle(
        pipeline_workflow_id(episode_id)
    ).fetch_history()

    replayer = Replayer(workflows=[EpisodePipelineWorkflow])
    await replayer.replay_workflow(history)
    await replayer.replay_workflow(resumed_history)


async def test_a_held_episode_parks_before_the_first_stage(
    real_pipeline: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session)
    await submit(session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    parked, _history = await _pipeline(
        real_pipeline, episode_id, start_stage="script", control_gate=True
    )
    assert parked["outcome"] == PipelineOutcome.PARKED.value
    assert parked["stopped_stage"] == "script"
    assert WORLD.stage_calls == []


async def test_the_default_run_records_no_patch_marker_and_no_control_gate_activity(
    real_pipeline: Env, session
) -> None:
    """既定（control_gate=False）は日次・旧履歴と同じ形（patch の marker も gate も無い）。"""
    episode_id = await make_episode(session)
    _result, history = await _pipeline(
        real_pipeline, episode_id, start_stage="script", control_gate=False
    )
    assert PIPELINE_CONTROL_GATE_ACTIVITY not in _activity_names(history)
    assert not _markers(history)
    assert CONTROL_GATE_PATCH_ID not in _patch_ids(history)
    assert WORLD.stage_calls[:3] == ["ScriptWorkflow", "StoryboardWorkflow", "ProductionWorkflow"]


@pytest.mark.parametrize("control_gate", [True])
async def test_the_control_gate_run_records_the_patch_marker(
    real_pipeline: Env, session, session_factory, artifact_store, control_gate: bool
) -> None:
    episode_id = await make_episode(session)
    _result, history = await _pipeline(
        real_pipeline, episode_id, start_stage="script", control_gate=control_gate
    )
    assert _patch_ids(history) == [CONTROL_GATE_PATCH_ID]


async def test_a_retried_control_gate_activity_emits_checkpoint_waiting_once(
    session, session_factory, artifact_store
) -> None:
    """INV-40/INV-17: Activity の retry・再実行で ``checkpoint.waiting`` が重複しない。"""
    episode_id = await make_episode(session)
    await submit(
        session_factory,
        artifact_store,
        episode_id,
        envelope(CommandType.SET_MODE, {"mode": "co_production"}),
    )
    pipeline = PipelineActivities(
        session_factory=session_factory, paused_env=False, uploads_paused_env=False
    )
    request = ControlGateRequest(episode_id=episode_id, stage="storyboard")
    first = await pipeline.control_gate(request)
    retried = await pipeline.control_gate(request)
    assert first.proceed is False and retried.proceed is False
    assert first.waiting_point == retried.waiting_point == "before_storyboard"
    events = await _events(session_factory, episode_id)
    assert events.count(InterventionEvent.CHECKPOINT_WAITING) == 1
