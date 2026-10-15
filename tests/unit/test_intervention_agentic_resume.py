"""介入 command の resume が agentic Episode では ``AgenticEpisodeWorkflow`` を起動する。

ADR-0043 §Consequences の負債: resume は常に ``EpisodePipelineWorkflow`` を起動したので、
停止・待機の後に
agent stage が走らなかった。判定は Activity（pin 済みの計画の有無）、workflow は結果で分岐するだけ。
各テストの理由: docs/testing/jpmas2-fixa-rationale.md
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest_asyncio
from temporalio import workflow
from temporalio.api.enums.v1 import EventType
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from contracts.agentic_pipeline import (
    AGENTIC_RESUME_PATCH_ID,
    PIPELINE_KIND_KEY,
    PinWorkflowPlanRequest,
    agentic_episode_workflow_id,
)
from contracts.intervention import CommandType, intervention_command_workflow_id
from contracts.pipeline import PipelineOptions, pipeline_workflow_id
from contracts.states import EpisodeStatus
from infrastructure.intervention.budget import BudgetPolicy
from tests.support.intervention import NOW, envelope, make_episode, receive, submit
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import InterventionCommandWorkflow
from workers.pipeline.agentic_activities import AgenticPipelineActivities


@dataclass
class World:
    #: (workflow 名, workflow id, 入力)
    started: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


WORLD = World()


class _Held:
    """start されたら release の signal まで走り続ける fake（実際の parked / 完了を模す）。"""

    name = ""

    def __init__(self) -> None:
        self.released = False

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        WORLD.started.append((self.name, workflow.info().workflow_id, payload))
        await workflow.wait_condition(lambda: self.released)
        return {"episode_id": payload["episode_id"], "outcome": "completed"}

    @workflow.signal(name="release")
    def release(self) -> None:
        self.released = True


@workflow.defn(name="EpisodePipelineWorkflow")
class HeldLegacy(_Held):
    name = "EpisodePipelineWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@workflow.defn(name="AgenticEpisodeWorkflow")
class HeldAgentic(_Held):
    name = "AgenticEpisodeWorkflow"

    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._run(payload)


@dataclass
class Env:
    client: Any
    queue: str
    held_queue: str


@pytest_asyncio.fixture
async def env(session_factory, artifact_store):
    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue, held_queue = f"intervention-{uuid.uuid4()}", f"held-{uuid.uuid4()}"
        intervention = InterventionActivities(
            session_factory=session_factory,
            store=artifact_store,
            budget_policy=BudgetPolicy(),
            pipeline_options=PipelineOptions(pipeline_task_queue=held_queue),
            command_task_queue=queue,
            clock=lambda: NOW,
        )
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[InterventionCommandWorkflow],
            activities=intervention.activities(),
        )
        held = Worker(
            wf_env.client,
            task_queue=held_queue,
            workflows=[HeldLegacy, HeldAgentic],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, held:
            with wf_env.auto_time_skipping_disabled():
                yield Env(wf_env.client, queue, held_queue)


async def _pin(session_factory, artifact_store, episode_id: str) -> None:
    await AgenticPipelineActivities(
        session_factory=session_factory, store=artifact_store, bucket="artifacts"
    ).pin_workflow_plan(
        PinWorkflowPlanRequest(
            episode_id=episode_id, template_id="shorts_agentic", template_version="1"
        )
    )


async def _resume(env: Env, session_factory, artifact_store, episode_id: str, *, hold: bool = True):
    """hold して resume の command workflow を走らせ、その結果と history を返す。"""
    if hold:
        await submit(
            session_factory, artifact_store, episode_id, envelope(CommandType.REQUEST_HOLD)
        )
    resume = envelope(CommandType.RESUME)
    await receive(session_factory, episode_id, resume)
    handle = await env.client.start_workflow(
        "InterventionCommandWorkflow",
        {"command_id": resume.command_id},
        id=intervention_command_workflow_id(resume.command_id),
        task_queue=env.queue,
    )
    return await handle.result(), await handle.fetch_history()


async def _release(env: Env, workflow_id: str) -> None:
    handle = env.client.get_workflow_handle(workflow_id)
    await handle.signal("release")
    await handle.result()


async def test_resume_of_an_agentic_episode_starts_the_agentic_workflow_not_the_pipeline(
    env: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await _pin(session_factory, artifact_store, episode_id)

    result, _history = await _resume(env, session_factory, artifact_store, episode_id)

    wid = agentic_episode_workflow_id(episode_id)
    assert result["pipeline"]["outcome"] == "started" and result["pipeline"]["workflow_id"] == wid
    ((name, started_id, payload),) = WORLD.started
    assert (name, started_id) == ("AgenticEpisodeWorkflow", wid)
    assert payload["episode_id"] == episode_id
    assert (payload["template_id"], payload["template_version"]) == ("shorts_agentic", "1")
    assert payload["options"]["pipeline_task_queue"] == env.held_queue
    await _release(env, wid)


async def test_resume_of_a_legacy_episode_is_unchanged(
    env: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)

    result, history = await _resume(env, session_factory, artifact_store, episode_id)

    assert result["pipeline"]["workflow_id"] == pipeline_workflow_id(episode_id)
    ((name, _id, payload),) = WORLD.started
    assert name == "EpisodePipelineWorkflow" and payload["start_stage"] is not None
    assert PIPELINE_KIND_KEY not in payload
    assert not _patch_ids(history)
    await _release(env, pipeline_workflow_id(episode_id))


async def test_a_second_resume_while_the_agentic_run_is_open_reports_already_running(
    env: Env, session, session_factory, artifact_store
) -> None:
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await _pin(session_factory, artifact_store, episode_id)
    await _resume(env, session_factory, artifact_store, episode_id)

    second, _ = await _resume(env, session_factory, artifact_store, episode_id, hold=False)

    assert second["pipeline"]["outcome"] == "already_running"
    assert len(WORLD.started) == 1
    await _release(env, agentic_episode_workflow_id(episode_id))


async def test_a_closed_agentic_run_can_be_started_again_by_the_next_resume(
    env: Env, session, session_factory, artifact_store
) -> None:
    """parked / 完了で閉じた実行の id を再利用できる（同時に2本は走らない）。"""
    episode_id = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await _pin(session_factory, artifact_store, episode_id)
    wid = agentic_episode_workflow_id(episode_id)
    await _resume(env, session_factory, artifact_store, episode_id)
    await _release(env, wid)  # 1本目が閉じる

    again, _ = await _resume(env, session_factory, artifact_store, episode_id)

    assert again["pipeline"]["outcome"] == "started"
    assert [n for n, _i, _p in WORLD.started] == ["AgenticEpisodeWorkflow"] * 2
    assert {i for _n, i, _p in WORLD.started} == {wid}
    await _release(env, wid)


async def test_the_agentic_branch_is_patched_and_both_histories_replay(
    env: Env, session, session_factory, artifact_store
) -> None:
    """INV-55: 分岐は patched で守る。legacy の履歴は marker を持たず、agentic の履歴は持つ。"""
    legacy_ep = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    _r, legacy_history = await _resume(env, session_factory, artifact_store, legacy_ep)
    agentic_ep = await make_episode(session, EpisodeStatus.SCRIPT_READY)
    await _pin(session_factory, artifact_store, agentic_ep)
    _r, agentic_history = await _resume(env, session_factory, artifact_store, agentic_ep)

    assert AGENTIC_RESUME_PATCH_ID in _patch_ids(agentic_history)
    assert AGENTIC_RESUME_PATCH_ID not in _patch_ids(legacy_history)
    replayer = Replayer(workflows=[InterventionCommandWorkflow])
    await replayer.replay_workflow(legacy_history)
    await replayer.replay_workflow(agentic_history)
    await _release(env, pipeline_workflow_id(legacy_ep))
    await _release(env, agentic_episode_workflow_id(agentic_ep))


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


async def test_a_history_recorded_before_the_agentic_branch_still_replays() -> None:
    """INV-55: 分岐を足す前の code で記録した resume の履歴（EpisodePipelineWorkflow を start）。"""
    path = (
        Path(__file__).parent / "fixtures" / "intervention_command_resume_pre_agentic_history.json"
    )
    history = WorkflowHistory.from_json("pre-agentic-resume", path.read_text("utf-8"))
    await Replayer(workflows=[InterventionCommandWorkflow]).replay_workflow(history)
