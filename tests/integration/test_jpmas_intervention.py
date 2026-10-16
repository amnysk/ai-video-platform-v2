"""JP-MAS-001 §17-3・5・9 と INV-49 を、実 PostgreSQL + 実 Temporal + 実 MinIO で確かめる。

単体試験（tests/unit/test_intervention_*.py, test_assistant_e2e.py）は SQLite・time-skipping で
同じ振る舞いを見ている。ここは**同じ主張を実 DB の行ロック・実 Temporal の履歴・実 bucket で**
言い直す（SQLite では FOR UPDATE が効かない、time-skipping では再送の実時間が無い）。理由は
docs/testing/jpmas-integration-rationale.md。

安全: スキーマは使い捨て（``*_test`` DB の一時スキーマ）、Temporal は ``TEST_TEMPORAL_NAMESPACE``、
bucket は ``TEST_MINIO_BUCKET``。有料 provider・YouTube には触れない（stage の workflow は fake）。
Worker は task queue ``intervention``（実 API の starter が使う固定名）を聞くので、同じ namespace に
並列で走らせない。
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from apps.api.auth import parse_principals
from apps.api.intervention_backend import InterventionModules, StoreBackedInterventionBackend
from apps.assistant.client import InterventionClient
from apps.assistant.tools import execute_tool
from contracts.assistant_tools import tool_command_id
from contracts.intervention import (
    INTERVENTION_TASK_QUEUE,
    CommandStatus,
    CommandType,
    ControlState,
    InterventionEvent,
    intervention_command_workflow_id,
)
from contracts.pipeline import EpisodePipelineInput, PipelineOutcome, pipeline_workflow_id
from contracts.states import EpisodeStatus
from infrastructure.config import Settings
from infrastructure.db.models import Base
from infrastructure.intervention.budget import BudgetPolicy
from infrastructure.intervention.store import InterventionStore
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.intervention import NOW, apply, envelope, make_episode, receive, submit
from tests.support.temporal import connect_test_client, temporal_test_address
from tests.unit import test_intervention_workflows as wf
from tests.unit.test_assistant_e2e import BridgeTransport
from tests.unit.test_intervention_api import (
    AI_TOKEN,
    FakeReadModel,
    _principals_json,
    fake_parse_proposal_file,
)
from workers.intervention.activities import InterventionActivities
from workers.intervention.workflows import (
    InterventionCommandWorkflow,
    InterventionRedeliveryWorkflow,
)
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.workflows import EpisodePipelineWorkflow

TEST_DATABASE_URL = require_test_database_url()
ADDRESS = temporal_test_address()

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT") or not ADDRESS,
    reason="TEST_DATABASE_URL (*_test), MINIO_ENDPOINT and TEMPORAL_ADDRESS are required",
)


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"jpmas_it_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(TEST_DATABASE_URL or "")
    async with admin.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        TEST_DATABASE_URL or "", connect_args={"options": f"-c search_path={schema}"}
    )
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def minio_store():
    from tests.support.minio import connect_test_artifact_store

    return await connect_test_artifact_store()


@pytest_asyncio.fixture
async def client() -> Client:
    return await connect_test_client()


class Stack:
    """実 Temporal 上の worker 群（command・再配送・pipeline・stage の fake）。"""

    def __init__(self, client: Client, command_queue: str, stage_queue: str, held_queue: str):
        self.client = client
        self.queue = command_queue
        self.stage_queue = stage_queue
        self.held_queue = held_queue

    def env(self) -> wf.Env:
        return wf.Env(self.client, self.queue, self.stage_queue, self.held_queue)


async def _stack(
    client: Client, session_factory, store, *, queue: str, real_pipeline: bool
) -> AsyncIterator[Stack]:
    wf.WORLD = wf.World()
    stage_queue = f"jpmas-stages-{uuid.uuid4().hex[:8]}"
    held_queue = f"jpmas-held-{uuid.uuid4().hex[:8]}"
    intervention = InterventionActivities(
        session_factory=session_factory,
        store=store,
        budget_policy=BudgetPolicy(),
        pipeline_options=wf._options(stage_queue, queue if real_pipeline else held_queue),
        command_task_queue=queue,
        clock=lambda: NOW,
    )
    pipeline = PipelineActivities(
        session_factory=session_factory, paused_env=False, uploads_paused_env=False
    )
    main = Worker(
        client,
        task_queue=queue,
        workflows=[InterventionCommandWorkflow, InterventionRedeliveryWorkflow]
        + ([EpisodePipelineWorkflow] if real_pipeline else []),
        activities=[*pipeline.activities(), *intervention.activities()],
    )
    held = Worker(
        client,
        task_queue=held_queue,
        workflows=[wf.HeldPipeline],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    stages = Worker(
        client,
        task_queue=stage_queue,
        workflows=[
            wf.FakeScript,
            wf.FakeStoryboard,
            wf.FakeProduction,
            wf.FakeRender,
            wf.FakeUpload,
        ],
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    async with main, held, stages:
        yield Stack(client, queue, stage_queue, held_queue)


@pytest_asyncio.fixture
async def held(client, pg_session_factory, minio_store) -> AsyncIterator[Stack]:
    """command workflow の検査用: pipeline は start の回数を数える fake（held queue）。"""
    queue = f"jpmas-cmd-{uuid.uuid4().hex[:8]}"
    async for stack in _stack(
        client, pg_session_factory, minio_store, queue=queue, real_pipeline=False
    ):
        yield stack


@pytest_asyncio.fixture
async def api_stack(client, pg_session_factory, minio_store) -> AsyncIterator[Stack]:
    """実 API の starter が start する固定 queue（``intervention``）を聞く worker。"""
    async for stack in _stack(
        client, pg_session_factory, minio_store, queue=INTERVENTION_TASK_QUEUE, real_pipeline=False
    ):
        yield stack


@pytest_asyncio.fixture
async def real_pipeline(client, pg_session_factory, minio_store) -> AsyncIterator[Stack]:
    """本物の EpisodePipelineWorkflow + control gate。stage は fake。"""
    queue = f"jpmas-pipe-{uuid.uuid4().hex[:8]}"
    async for stack in _stack(
        client, pg_session_factory, minio_store, queue=queue, real_pipeline=True
    ):
        yield stack


async def _run(stack: Stack, command_id: str, *, suffix: str = "") -> dict[str, Any]:
    return await stack.client.execute_workflow(
        "InterventionCommandWorkflow",
        {"command_id": command_id},
        id=intervention_command_workflow_id(command_id) + suffix,
        task_queue=stack.queue,
        result_type=dict,
        run_timeout=timedelta(seconds=60),
    )


async def _release(stack: Stack, episode_id: str) -> None:
    await stack.client.get_workflow_handle(pipeline_workflow_id(episode_id)).signal("release")


async def _events(factory, episode_id: str) -> list[InterventionEvent]:
    async with factory() as s:
        return [e.event_name for e in await InterventionStore(s).list_events(episode_id)]


async def _control(factory, episode_id: str):
    async with factory() as s:
        return await InterventionStore(s).get_control(episode_id)


# --------------------------------------------------------------------------- §17-3


async def test_a_resent_command_does_not_apply_or_dispatch_twice_on_a_real_stack(
    held: Stack, pg_session_factory, minio_store
) -> None:
    """§17-3: 同じ command_id の再送・別 workflow id での再実行・再配送で、変更も生成も増えない。"""
    async with pg_session_factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.SCRIPT_READY)
    await submit(pg_session_factory, minio_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    resume = envelope(CommandType.RESUME)
    await receive(pg_session_factory, episode_id, resume)
    first = await _run(held, resume.command_id)
    assert first["status"] == CommandStatus.APPLIED.value
    assert first["pipeline"]["outcome"] == "started"
    revision = (await _control(pg_session_factory, episode_id)).control_revision

    again = await _run(held, resume.command_id, suffix="-resend")
    assert again["replayed"] is True
    assert again["pipeline"] == first["pipeline"]
    assert (await _control(pg_session_factory, episode_id)).control_revision == revision
    assert (await _events(pg_session_factory, episode_id)).count(
        InterventionEvent.RESUME_REQUESTED
    ) == 1
    assert len(wf.WORLD.pipeline_inputs) == 1  # pipeline の start は1回
    async with pg_session_factory() as s:
        commands = await InterventionStore(s).list_commands(episode_id)
    assert [c.command_id for c in commands].count(resume.command_id) == 1

    # 再配送 workflow は配送済みの command を拾い直さない
    redelivered = await held.client.execute_workflow(
        "InterventionRedeliveryWorkflow",
        {},
        id=f"redelivery-{uuid.uuid4()}",
        task_queue=held.queue,
        result_type=dict,
    )
    assert resume.command_id not in redelivered["started"]
    assert len(wf.WORLD.pipeline_inputs) == 1
    await _release(held, episode_id)


# --------------------------------------------------------------------------- §17-9


async def test_a_crash_after_the_db_apply_recovers_to_one_change_on_a_real_stack(
    held: Stack, pg_session_factory, minio_store
) -> None:
    """§17-9 / INV-42: DB の apply 後に workflow が落ちても、再確認で1回の変更になる。"""
    async with pg_session_factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.SCRIPT_READY)
    await submit(pg_session_factory, minio_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    resume = envelope(CommandType.RESUME)
    await receive(pg_session_factory, episode_id, resume)
    applied = await apply(pg_session_factory, minio_store, resume.command_id)  # ここで「落ちた」
    revision = (await _control(pg_session_factory, episode_id)).control_revision

    recovered = await _run(held, resume.command_id)
    assert recovered["replayed"] is True
    assert recovered["pipeline"]["outcome"] == "started"
    control = await _control(pg_session_factory, episode_id)
    assert control.control_revision == revision
    async with pg_session_factory() as s:
        command = await InterventionStore(s).get_command(resume.command_id)
    assert command is not None and command.result is not None
    assert command.result["start_stage"] == applied.result["start_stage"]
    assert (await _events(pg_session_factory, episode_id)).count(
        InterventionEvent.RESUME_REQUESTED
    ) == 1
    assert len(wf.WORLD.pipeline_inputs) == 1
    await _release(held, episode_id)


async def test_concurrent_applies_of_one_command_change_the_database_once(
    pg_session_factory, minio_store
) -> None:
    """§17-3/9 の下地: 実 PostgreSQL の行ロック。同じ command の apply を同時に走らせても1回。

    SQLite は FOR UPDATE を無視するので、この主張は実 DB でしか確かめられない。
    """
    async with pg_session_factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.SCRIPT_READY)
    await submit(pg_session_factory, minio_store, episode_id, envelope(CommandType.REQUEST_HOLD))
    resume = envelope(CommandType.RESUME)
    await receive(pg_session_factory, episode_id, resume)
    outcomes = await asyncio.gather(
        *[apply(pg_session_factory, minio_store, resume.command_id) for _ in range(4)],
        return_exceptions=True,
    )
    assert not [o for o in outcomes if isinstance(o, BaseException)], outcomes
    assert (await _events(pg_session_factory, episode_id)).count(
        InterventionEvent.RESUME_REQUESTED
    ) == 1
    async with pg_session_factory() as s:
        command = await InterventionStore(s).get_command(resume.command_id)
    assert command is not None and command.status is CommandStatus.APPLIED


# --------------------------------------------------------------------------- §17-5


async def test_a_received_hold_is_not_effective_and_an_effective_hold_dispatches_nothing(
    real_pipeline: Stack, pg_session_factory, minio_store
) -> None:
    """§17-5: 停止の受付と有効化は別。有効化後は実 Temporal の pipeline が新規 stage を始めない。"""
    async with pg_session_factory() as s:
        episode_id = await make_episode(s)
    hold = envelope(CommandType.REQUEST_HOLD)
    await receive(pg_session_factory, episode_id, hold)  # 受付だけ（未適用）
    async with pg_session_factory() as s:
        command = await InterventionStore(s).get_command(hold.command_id)
    assert command is not None and command.status is CommandStatus.RECEIVED
    # 受付では「停止要求あり」までで、有効（HELD）ではない。有効化の事実もまだ記録されない
    assert (
        await _control(pg_session_factory, episode_id)
    ).control_state is ControlState.HOLD_REQUESTED
    received_events = await _events(pg_session_factory, episode_id)
    assert InterventionEvent.HOLD_REQUESTED in received_events
    assert InterventionEvent.HOLD_EFFECTIVE not in received_events

    applied = await apply(pg_session_factory, minio_store, hold.command_id)  # 有効化
    assert applied.status is CommandStatus.APPLIED
    assert (await _control(pg_session_factory, episode_id)).control_state is ControlState.HELD
    assert InterventionEvent.HOLD_EFFECTIVE in await _events(pg_session_factory, episode_id)

    handle = await real_pipeline.client.start_workflow(
        "EpisodePipelineWorkflow",
        EpisodePipelineInput(
            episode_id=episode_id,
            options=wf._options(real_pipeline.stage_queue, real_pipeline.queue),
            start_stage="script",
            control_gate=True,
        ),
        id=pipeline_workflow_id(episode_id),
        task_queue=real_pipeline.queue,
    )
    result = await asyncio.wait_for(handle.result(), timeout=60)
    assert result["outcome"] == PipelineOutcome.PARKED.value
    assert result["stopped_stage"] == "script"
    assert wf.WORLD.stage_calls == []  # 新規の生成・投稿を始めていない


# --------------------------------------------------------------------------- INV-49 adapter


@pytest_asyncio.fixture
async def api(api_stack: Stack, pg_session_factory, minio_store):
    """実 API（実 receive_command・実 starter）に、同じ process の橋で adapter を繋ぐ。"""
    from apps.api.main import create_app
    from apps.api.routers import intervention
    from infrastructure.intervention import commands

    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        temporal_address=ADDRESS or "",
        temporal_namespace=os.environ.get("TEST_TEMPORAL_NAMESPACE", "avp-test"),
    )
    from apps.api.intervention_backend import TemporalCommandStarter

    modules = InterventionModules(
        receive_command=commands.receive_command,
        make_principal=lambda actor, scopes: commands.Principal(actor=actor, scopes=scopes),
        read_model=lambda: FakeReadModel([]),
        build_packet=lambda view: {},
        render_markdown=lambda packet: "",
        parse_proposal_file=fake_parse_proposal_file,
        command_starter=TemporalCommandStarter(settings, pg_session_factory),
        receive_error_types=(commands.CommandReceiveError,),
    )
    backend = StoreBackedInterventionBackend(pg_session_factory, modules)
    app = create_app()
    registry = parse_principals(_principals_json())
    app.dependency_overrides[intervention.get_principal_registry] = lambda: registry
    app.dependency_overrides[intervention.get_intervention_backend] = lambda: backend
    yield app


async def _call(app, tool: str, args: dict[str, Any], call: str, session: str = "sess-it"):
    loop = asyncio.get_running_loop()
    adapter = InterventionClient(
        "http://test", AI_TOKEN, session_id=session, transport=BridgeTransport(app, loop)
    )
    try:
        return await asyncio.to_thread(execute_tool, adapter, tool, args, tool_call_id=call)
    finally:
        adapter.close()


async def _wait_applied(factory, command_id: str, timeout: float = 30.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with factory() as s:
            command = await InterventionStore(s).get_command(command_id)
        if command is not None and command.status is CommandStatus.APPLIED:
            return command
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"command {command_id} not applied: {command}")
        await asyncio.sleep(0.2)


async def test_a_retried_assistant_tool_call_is_one_logical_change_end_to_end(
    api, api_stack: Stack, pg_session_factory
) -> None:
    """INV-49: 実 PG・実 Temporal を通しても、tool 呼び出しの再試行は1つの変更。"""
    async with pg_session_factory() as s:
        episode_id = await make_episode(s, EpisodeStatus.SCRIPT_READY)
    args = {"episode_id": episode_id, "reason": "確認"}
    first = await _call(api, "request_hold", args, "call-hold")
    command_id = tool_command_id("sess-it", "call-hold")
    assert first["status"] == "submitted" and first["command_id"] == command_id
    applied = await _wait_applied(pg_session_factory, command_id)
    revision = (await _control(pg_session_factory, episode_id)).control_revision

    again = await _call(api, "request_hold", args, "call-hold")  # 再試行
    assert again["command_id"] == command_id and again["replayed"] is True
    await asyncio.sleep(1.0)  # 二度目の workflow が動いても増えないことを待って確かめる
    async with pg_session_factory() as s:
        rows = await InterventionStore(s).list_commands(episode_id)
    assert [c.command_id for c in rows] == [command_id]
    assert rows[0].actor.type.value == "ai_assistant"
    assert applied.status is CommandStatus.APPLIED
    control = await _control(pg_session_factory, episode_id)
    assert control.control_revision == revision
    assert control.control_state is ControlState.HELD
    events = await _events(pg_session_factory, episode_id)
    assert events.count(InterventionEvent.HOLD_REQUESTED) == 1
    assert events.count(InterventionEvent.HOLD_EFFECTIVE) == 1

    other = await _call(
        api,
        "add_comment",
        {"episode_id": episode_id, "anchor": {"segment_id": "s1"}, "body": "短く"},
        "call-2",
    )
    clash = await _call(
        api,
        "add_comment",
        {"episode_id": episode_id, "anchor": {"segment_id": "s1"}, "body": "別"},
        "call-2",
    )
    assert other["status"] == "submitted"
    assert clash["status"] == "command_id_reused"
    async with pg_session_factory() as s:
        assert len(await InterventionStore(s).list_commands(episode_id)) == 2
