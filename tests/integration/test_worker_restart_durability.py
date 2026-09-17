"""durable execution は worker プロセスの寿命に依存しない（実プロセス・SIGKILL・実 Temporal）。

- worker は ``python -m tests.support.durability.*`` の子プロセス。一意な task queue だけを使う
- 工程の途中で SIGKILL → 新しいプロセスを起動 → 完了し、Episode・工程・投稿は1回だけ
- DB は実 PostgreSQL の一時スキーマ。YouTube は状態をファイルに残す fake。有料 API に出ない
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import create_async_engine
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError

from contracts.pipeline import (
    DailyEpisodeInput,
    PipelineOptions,
    pipeline_workflow_id,
    production_workflow_id,
    render_workflow_id,
    script_workflow_id,
    storyboard_workflow_id,
    upload_workflow_id,
)
from contracts.states import EpisodeStatus, ProviderCall, ReservationStatus
from infrastructure.db.models import Base, DailyEpisodeSlotRow, EpisodeRow, ProviderReservationRow
from tests.support.db import assert_destructive_allowed, require_test_database_url
from tests.support.durability.common import (
    ENV_BLOCK_STAGES,
    ENV_CHUNK_DELAY,
    ENV_DB_URL,
    ENV_QUEUE,
    ENV_SCHEMA,
    ENV_STAGE_QUEUE,
    ENV_STATE_FILE,
    ENV_WORK_DIR,
    WorkerProcess,
    schema_session_factory,
)
from tests.support.durability.file_uploader import read_state
from tests.support.upload import TEST_CHUNK_BYTES, seed_render_ready
from workers.upload.workflows import UploadWorkflowInput

TEST_DATABASE_URL = require_test_database_url()
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not TEMPORAL_ADDRESS,
    reason="TEST_DATABASE_URL (*_test) and TEMPORAL_ADDRESS are required",
)


@pytest_asyncio.fixture
async def schema():
    name = f"durability_{uuid.uuid4().hex[:12]}"
    url = TEST_DATABASE_URL or ""
    admin = create_async_engine(url)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{name}"'))
    except Exception as exc:  # noqa: BLE001 - DB 不達は skip
        await admin.dispose()
        pytest.skip(f"test database not reachable: {type(exc).__name__}")
    factory = schema_session_factory(url, name)
    try:
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with factory.kw["bind"].begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield name, factory
    finally:
        await factory.kw["bind"].dispose()
        assert_destructive_allowed(TEST_DATABASE_URL)
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{name}" CASCADE'))
        await admin.dispose()


@pytest_asyncio.fixture
async def client() -> Client:
    try:
        return await asyncio.wait_for(
            Client.connect(TEMPORAL_ADDRESS or "", namespace="default"), timeout=5
        )
    except Exception as exc:  # noqa: BLE001 - 到達不能なら skip
        pytest.skip(f"Temporal not reachable: {type(exc).__name__}")


@pytest.fixture
def procs():
    started: list[WorkerProcess] = []
    yield started
    for p in started:
        p.kill()


async def _wait_for(predicate, *, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


async def _status(client: Client, workflow_id: str) -> WorkflowExecutionStatus | None:
    try:
        return (await client.get_workflow_handle(workflow_id).describe()).status
    except RPCError:
        return None


async def _runs(client: Client, workflow_id: str) -> list[WorkflowExecutionStatus | None]:
    return [w.status async for w in client.list_workflows(f"WorkflowId = '{workflow_id}'")]


async def _assert_single_completed_run(client: Client, workflow_id: str) -> None:
    async def settled():
        runs = await _runs(client, workflow_id)
        return (
            runs if runs and all(s is not WorkflowExecutionStatus.RUNNING for s in runs) else None
        )

    runs = await _wait_for(settled, timeout=20, what=f"visibility of {workflow_id}")
    assert runs == [WorkflowExecutionStatus.COMPLETED], (workflow_id, runs)


class PipelineStack:
    def __init__(
        self, schema_name: str, factory, client: Client, tmp_path: Path, procs, block: str
    ) -> None:
        suffix = uuid.uuid4().hex[:10]
        self.factory = factory
        self.client = client
        self.tmp_path = tmp_path
        self.procs = procs
        self.queue = f"durability-pipeline-{suffix}"
        self.stage_queue = f"durability-stages-{suffix}"
        self.env = {
            ENV_DB_URL: TEST_DATABASE_URL or "",
            ENV_SCHEMA: schema_name,
            ENV_QUEUE: self.queue,
            ENV_STAGE_QUEUE: self.stage_queue,
            ENV_BLOCK_STAGES: block,
            "TEMPORAL_ADDRESS": TEMPORAL_ADDRESS or "",
        }
        # 過去日付でも未来日付でもよい。同じスキーマは他のテストと共有しない
        self.slot_date = "2026-09-16"

    def worker(self) -> WorkerProcess:
        p = WorkerProcess(
            "tests.support.durability.pipeline_worker",
            self.env,
            self.tmp_path / f"pipeline-worker-{len(self.procs)}.log",
        ).start()
        self.procs.append(p)
        return p

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

    async def daily(self, trigger_id: str) -> dict:
        handle = await self.client.start_workflow(
            "DailyEpisodeWorkflow",
            DailyEpisodeInput(daily_limit=1, slot_date=self.slot_date, options=self.options()),
            id=trigger_id,
            task_queue=self.queue,
            result_type=dict,
        )
        return await asyncio.wait_for(handle.result(), timeout=60)

    async def counts(self) -> tuple[int, int, list[str]]:
        async with self.factory() as session:
            slots = await session.scalar(
                select(func.count())
                .select_from(DailyEpisodeSlotRow)
                .where(DailyEpisodeSlotRow.slot_date == date.fromisoformat(self.slot_date))
            )
            episodes = (await session.scalars(select(EpisodeRow))).all()
        return int(slots or 0), len(episodes), [e.status for e in episodes]

    async def wait_running(self, workflow_id: str) -> None:
        async def running():
            return await _status(self.client, workflow_id) is WorkflowExecutionStatus.RUNNING

        await _wait_for(running, timeout=30, what=f"{workflow_id} running")

    async def pipeline_result(self, episode_id: str) -> dict:
        handle = self.client.get_workflow_handle(pipeline_workflow_id(episode_id), result_type=dict)
        return await asyncio.wait_for(handle.result(), timeout=90)


async def test_pipeline_survives_worker_sigkill_mid_stage(schema, client, tmp_path, procs) -> None:
    name, factory = schema
    stack = PipelineStack(name, factory, client, tmp_path, procs, block="StoryboardWorkflow")
    first = stack.worker()
    trigger = f"durability-daily-{uuid.uuid4().hex[:10]}"

    started = await stack.daily(trigger)
    assert started["outcome"] == "started", started
    ep = started["episode_id"]
    await stack.wait_running(storyboard_workflow_id(ep))

    # Storyboard の途中（Script 完了・claim commit 済み）でプロセスごと殺す
    first.kill()
    assert not first.alive()
    assert await _status(client, pipeline_workflow_id(ep)) is WorkflowExecutionStatus.RUNNING
    await client.get_workflow_handle(storyboard_workflow_id(ep)).signal("release")

    second = stack.worker()
    assert second.pid != first.pid
    result = await stack.pipeline_result(ep)

    assert result["outcome"] == "completed", (result, second.log_tail())
    assert result["status"] == EpisodeStatus.UPLOADED.value
    assert result["completed_stages"] == ["script", "storyboard", "production", "render", "upload"]
    slots, episodes, statuses = await stack.counts()
    assert (slots, episodes) == (1, 1)
    assert statuses == [EpisodeStatus.UPLOADED.value]
    for wf_id in (
        trigger,
        pipeline_workflow_id(ep),
        script_workflow_id(ep),
        storyboard_workflow_id(ep),
        production_workflow_id(ep),
        render_workflow_id(ep),
        upload_workflow_id(ep),
    ):
        await _assert_single_completed_run(client, wf_id)


async def test_no_duplicate_episode_when_killed_after_claim_and_retriggered(
    schema, client, tmp_path, procs
) -> None:
    name, factory = schema
    stack = PipelineStack(name, factory, client, tmp_path, procs, block="ScriptWorkflow")
    first = stack.worker()
    trigger = f"durability-daily-{uuid.uuid4().hex[:10]}"

    started = await stack.daily(trigger)
    assert started["outcome"] == "started", started
    ep = started["episode_id"]
    await stack.wait_running(script_workflow_id(ep))
    assert (await stack.counts())[:2] == (1, 1)

    first.kill()
    stack.worker()

    # 同じ trigger の再実行（Schedule の再試行相当）と、同じ日の別 trigger（手動の再起動相当）
    same = await stack.daily(trigger)
    other = await stack.daily(f"durability-daily-{uuid.uuid4().hex[:10]}")
    for again in (same, other):
        assert again["outcome"] == "already_started", again
        assert again["episode_id"] == ep
    assert (await stack.counts())[:2] == (1, 1)

    await client.get_workflow_handle(script_workflow_id(ep)).signal("release")
    result = await stack.pipeline_result(ep)
    assert result["outcome"] == "completed", result
    slots, episodes, statuses = await stack.counts()
    assert (slots, episodes, statuses) == (1, 1, [EpisodeStatus.UPLOADED.value])
    await _assert_single_completed_run(client, pipeline_workflow_id(ep))
    await _assert_single_completed_run(client, script_workflow_id(ep))


#: 25 チャンク × 0.4 秒: kill が送信の途中に確実に落ちる長さ
UPLOAD_PAYLOAD = bytes(range(256))[: TEST_CHUNK_BYTES * 24 + 5]


@pytest.mark.skipif(not os.environ.get("MINIO_ENDPOINT"), reason="MINIO_ENDPOINT is required")
async def test_upload_resumes_after_worker_sigkill_without_second_video(
    schema, client, tmp_path, procs
) -> None:
    from infrastructure.config import Settings
    from infrastructure.storage.minio_store import MinioArtifactStore

    name, factory = schema
    store = MinioArtifactStore.from_settings(Settings())
    await store.ensure_bucket()
    episode_id = await seed_render_ready(factory, store, tmp_path, payload=UPLOAD_PAYLOAD)

    queue = f"durability-upload-{uuid.uuid4().hex[:10]}"
    state_file = tmp_path / "fake-youtube.pickle"
    env = {
        ENV_DB_URL: TEST_DATABASE_URL or "",
        ENV_SCHEMA: name,
        ENV_QUEUE: queue,
        ENV_STATE_FILE: str(state_file),
        ENV_WORK_DIR: str(tmp_path / "work"),
        ENV_CHUNK_DELAY: "0.4",
        "TEMPORAL_ADDRESS": TEMPORAL_ADDRESS or "",
    }

    def worker(i: int) -> WorkerProcess:
        p = WorkerProcess(
            "tests.support.durability.upload_worker", env, tmp_path / f"upload-worker-{i}.log"
        ).start()
        procs.append(p)
        return p

    first = worker(0)
    handle = await client.start_workflow(
        "UploadWorkflow",
        UploadWorkflowInput(episode_id=episode_id, upload_task_queue=f"{queue}-media"),
        id=upload_workflow_id(episode_id),
        task_queue=queue,
    )

    async def mid_send():
        state = read_state(state_file)
        return state.get("chunk_sends", 0) >= 3

    await _wait_for(mid_send, timeout=60, what="chunks being sent")
    first.kill()
    sent_before = read_state(state_file)["chunk_sends"]
    assert read_state(state_file)["videos"] == {}

    # 送信中の Activity は heartbeat timeout（60秒）+ retry 間隔（30秒）の後に別プロセスで再開する
    second = worker(1)
    result = await asyncio.wait_for(handle.result(), timeout=150)

    assert result["status"] == EpisodeStatus.UPLOADED.value, (result, second.log_tail())
    final = read_state(state_file)
    assert len(final["videos"]) == 1
    assert final["sessions_started"] == 1
    assert final["chunk_sends"] > sent_before
    async with factory() as session:
        rows = (
            await session.scalars(
                select(ProviderReservationRow).where(
                    ProviderReservationRow.provider == ProviderCall.YOUTUBE_UPLOAD.value
                )
            )
        ).all()
        episode = await session.get(EpisodeRow, uuid.UUID(episode_id))
    assert len(rows) == 1
    assert rows[0].status == ReservationStatus.SPENT.value and rows[0].round == 1
    assert episode is not None and episode.status == EpisodeStatus.UPLOADED.value
    await _assert_single_completed_run(client, upload_workflow_id(episode_id))
