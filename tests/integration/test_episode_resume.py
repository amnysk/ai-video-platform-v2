"""統一再開エントリポイント（ADR-0032）: 実 Temporal + 実 PostgreSQL。

**安全上の制約（実装時に判明。ここに明記する）**: この docker-compose 環境は同じ
Temporal サーバ上で本物の ``production-image`` / ``production-video`` /
``production-voice`` / render / upload の worker が常駐し、固定の task queue を
listen している。``EpisodePipelineWorkflow`` / ``PipelineOptions`` は各工程の
**state workflow の名前と queue** は差し替えられる
（``workers/pipeline/workflows.py::_stage_request``）が、その内部が使うメディア
Activity の task queue（``ProductionWorkflowInput.image_task_queue`` 等）は
``_stage_request`` が配線していないため差し替えられない。したがって本物の
``ProductionWorkflow`` / ``RenderWorkflow`` / ``UploadWorkflow`` を
``EpisodePipelineWorkflow`` 経由でこの共有サーバに対して実行すると、本物の worker が
拾って実際の外部 provider（有料の画像・動画生成 / YouTube）へ到達しうる
（AGENTS.md §9 違反のリスク）。

この既知の制約を避けるため、production/render/upload は**専用 queue に登録した
fake workflow**で置き換える（``tests/support/durability/stages.py`` と同じ安全策）。
「同じ input で二度課金しない」という核心の安全性そのもの（本物の
``ProductionWorkflow`` + 完全に差し替え可能な独自 queue）は
``tests/integration/test_production_rerun.py::test_failed_production_resumes_on_post_without_new_paid_submits``
が既に検査している。ここで検査するのは、統一再開（``GET/POST /episodes/{id}/resume``）が
実 DB・実 Temporal に対して:

(a) dry-run が書き込み・workflow 起動をしないこと
(b) production が blocked で止まった Episode から、resume が render → upload まで
    到達し、かつ「未完了だった分だけ」を作り直す（作り直し済みの分を重複生成しない）こと
    （fake が生成呼び出し数を記録する。本物の input_hash 判定そのものは上記の
    production_rerun テストが担う）
(c) 同時2回の POST が ``EpisodePipelineWorkflow`` を1本しか起動しないこと
(d) resume が ``daily_episode_slots`` に行を作らないこと
(e) resume でも ``UPLOADS_PAUSED`` ゲートが効くこと

を実際に確かめることである。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from contracts.operations import OperationalSwitch
from contracts.pipeline import (
    EpisodePipelineInput,
    PipelineOptions,
    pipeline_workflow_id,
    production_workflow_id,
)
from contracts.states import ArtifactType, EpisodeStatus
from domain.episode.transitions import EpisodeEvent
from infrastructure.db.models import Base, DailyEpisodeSlotRow
from infrastructure.db.repositories import (
    ArtifactMetadataRepository,
    EpisodeRepository,
    OperationalSwitchRepository,
)
from tests.support.db import assert_destructive_allowed, require_test_database_url
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.workflows import EpisodePipelineWorkflow

TEST_DATABASE_URL = require_test_database_url()
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL or not TEMPORAL_ADDRESS,
    reason="TEST_DATABASE_URL (*_test) and TEMPORAL_ADDRESS are required",
)


# --------------------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def pg_session_factory():
    schema = f"resume_it_{uuid.uuid4().hex[:12]}"
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
async def client() -> Client:
    try:
        return await asyncio.wait_for(
            Client.connect(TEMPORAL_ADDRESS or "", namespace="default"), timeout=5
        )
    except Exception as exc:  # noqa: BLE001 - 到達不能なら skip
        pytest.skip(f"Temporal not reachable: {type(exc).__name__}")


# --------------------------------------------------------------------------- fake 工程

FAKE_PRODUCTION_ACTIVITY = "resume_it_fake_production"
FAKE_RENDER_ACTIVITY = "resume_it_fake_render"
FAKE_UPLOAD_ACTIVITY = "resume_it_fake_upload"


class World:
    def __init__(self) -> None:
        self.production_attempts = 0
        #: 「新たに生成した」呼び出し数の模し（本物は fal 呼び出し。ここでは整数を積むだけ）
        self.image_submit_calls = 0
        self.render_calls = 0
        self.upload_calls = 0


def _fake_production_activity(factory, world: World):
    @activity.defn(name=FAKE_PRODUCTION_ACTIVITY)
    async def run(episode_id: str) -> str:
        async with factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(episode_id)
            assert episode is not None
            if episode.status is EpisodeStatus.STORYBOARD_READY:
                await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
                await episodes.set_workflow_id(
                    episode_id, f"{production_workflow_id(episode_id)}:run-1"
                )
            elif episode.status is EpisodeStatus.BLOCKED:
                await episodes.apply_event(episode_id, EpisodeEvent.RESUMED)
            else:
                raise AssertionError(f"unexpected status for fake production: {episode.status}")

            world.production_attempts += 1
            if world.production_attempts == 1:
                # 4シーン中3シーンだけ生成が済んだところで voice が壊れて blocked になる、
                # という production_rerun.py と同じ筋書きを模す
                world.image_submit_calls += 3
                await episodes.apply_event(episode_id, EpisodeEvent.NEEDS_INPUT_FAILURE)
                result = EpisodeStatus.BLOCKED.value
            else:
                # 再開: 残り1シーンだけ生成する（3シーン分は再生成しない）
                world.image_submit_calls += 1
                await episodes.apply_event(episode_id, EpisodeEvent.ASSETS_READY)
                result = EpisodeStatus.ASSETS_READY.value
            await session.commit()
        return result

    return run


def _fake_render_activity(factory, world: World):
    @activity.defn(name=FAKE_RENDER_ACTIVITY)
    async def run(episode_id: str) -> str:
        async with factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(episode_id)
            assert episode is not None and episode.status is EpisodeStatus.ASSETS_READY, episode
            await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
            await episodes.apply_event(episode_id, EpisodeEvent.RENDER_READY)
            await ArtifactMetadataRepository(session).record(
                episode_id=episode_id,
                artifact_type=ArtifactType.FINAL_VIDEO,
                schema_version="resume-it-fake",
                bucket="artifacts",
                object_key=f"resume-it/{episode_id}/final.mp4",
                sha256="e" * 64,
            )
            await session.commit()
        world.render_calls += 1
        return EpisodeStatus.RENDER_READY.value

    return run


def _fake_upload_activity(factory, world: World):
    @activity.defn(name=FAKE_UPLOAD_ACTIVITY)
    async def run(episode_id: str) -> str:
        async with factory() as session:
            episodes = EpisodeRepository(session)
            episode = await episodes.get(episode_id)
            assert episode is not None and episode.status is EpisodeStatus.RENDER_READY, episode
            await episodes.apply_event(episode_id, EpisodeEvent.STAGE_ADMITTED)
            await episodes.apply_event(episode_id, EpisodeEvent.UPLOAD_SUCCEEDED)
            await session.commit()
        world.upload_calls += 1
        return EpisodeStatus.UPLOADED.value

    return run


@workflow.defn(name="ResumeItFakeProduction")
class FakeProductionWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        ep = payload["episode_id"]
        from datetime import timedelta

        status = await workflow.execute_activity(
            FAKE_PRODUCTION_ACTIVITY, args=[ep], start_to_close_timeout=timedelta(seconds=20)
        )
        return {"episode_id": ep, "status": status}


@workflow.defn(name="ResumeItFakeRender")
class FakeRenderWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        ep = payload["episode_id"]
        from datetime import timedelta

        status = await workflow.execute_activity(
            FAKE_RENDER_ACTIVITY, args=[ep], start_to_close_timeout=timedelta(seconds=20)
        )
        return {"episode_id": ep, "status": status}


@workflow.defn(name="ResumeItFakeUpload")
class FakeUploadWorkflow:
    @workflow.run
    async def run(self, payload: dict) -> dict:
        ep = payload["episode_id"]
        from datetime import timedelta

        status = await workflow.execute_activity(
            FAKE_UPLOAD_ACTIVITY, args=[ep], start_to_close_timeout=timedelta(seconds=20)
        )
        return {"episode_id": ep, "status": status}


class QueueScopedStarter:
    """``apps.api.workflow_starter.WorkflowStarter`` の test double。

    production 用の ``TemporalWorkflowStarter`` は ``EPISODE_PIPELINE_WORKFLOW`` の
    固定 queue（``pipeline``）を使う。本物の docker-compose worker が同じ queue を
    listen しているため、このテストでは使わない（上のモジュール docstring 参照）。
    ここでは一意な queue へ差し替える以外、``POST /episodes/{id}/resume`` ハンドラが
    呼ぶのと同じ ``start_pipeline_workflow`` の形だけを実装する。
    """

    def __init__(self, client: Client, queue: str, options: PipelineOptions) -> None:
        self._client = client
        self._queue = queue
        self._options = options

    async def start_episode_workflow(self, *, episode_id: str, pipeline: str = "skeleton") -> str:
        raise AssertionError("resume must not start the creation pipeline")

    async def start_storyboard_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start storyboard directly")

    async def start_production_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start production directly")

    async def start_render_workflow(self, *, episode_id: str, render_profile_id: str = "x") -> str:
        raise AssertionError("resume must not start render directly")

    async def start_upload_workflow(self, *, episode_id: str) -> str:
        raise AssertionError("resume must not start upload directly")

    async def start_pipeline_workflow(self, *, episode_id: str, start_stage: str) -> str:
        workflow_id = pipeline_workflow_id(episode_id)
        await self._client.start_workflow(
            "EpisodePipelineWorkflow",
            EpisodePipelineInput(
                episode_id=episode_id, start_stage=start_stage, options=self._options
            ),
            id=workflow_id,
            task_queue=self._queue,
        )
        return workflow_id


class Stack:
    def __init__(self, factory, client: Client) -> None:
        self.factory = factory
        self.client = client
        self.world = World()
        self.queue = f"resume-it-{uuid.uuid4().hex[:10]}"
        self.pipeline_activities = PipelineActivities(
            session_factory=factory, paused_env=False, uploads_paused_env=False
        )
        self.options = PipelineOptions(
            production_workflow=("ResumeItFakeProduction", self.queue),
            render_workflow=("ResumeItFakeRender", self.queue),
            upload_workflow=("ResumeItFakeUpload", self.queue),
            pipeline_task_queue=self.queue,
        )
        self.starter = QueueScopedStarter(client, self.queue, self.options)

    def worker(self) -> Worker:
        return Worker(
            self.client,
            task_queue=self.queue,
            workflows=[
                EpisodePipelineWorkflow,
                FakeProductionWorkflow,
                FakeRenderWorkflow,
                FakeUploadWorkflow,
            ],
            activities=[
                *self.pipeline_activities.activities(),
                _fake_production_activity(self.factory, self.world),
                _fake_render_activity(self.factory, self.world),
                _fake_upload_activity(self.factory, self.world),
            ],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )

    async def app_client(self) -> AsyncClient:
        from apps.api.dependencies import get_session_factory, get_workflow_starter
        from apps.api.main import create_app

        app = create_app()
        app.dependency_overrides[get_session_factory] = lambda: self.factory
        app.dependency_overrides[get_workflow_starter] = lambda: self.starter
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def pipeline_result(self, episode_id: str) -> dict:
        handle = self.client.get_workflow_handle(pipeline_workflow_id(episode_id), result_type=dict)
        return await asyncio.wait_for(handle.result(), timeout=30)


@pytest_asyncio.fixture
async def stack(pg_session_factory, client) -> Stack:
    return Stack(pg_session_factory, client)


# --------------------------------------------------------------------------- helpers


async def _episode_at(factory, events: list[EpisodeEvent]) -> str:
    async with factory() as session:
        episodes = EpisodeRepository(session)
        episode = await episodes.create(topic="resume-it")
        for event in events:
            await episodes.apply_event(episode.id, event)
        await session.commit()
        return episode.id


TO_STORYBOARD_READY = [
    EpisodeEvent.WORKFLOW_STARTED,
    EpisodeEvent.SCRIPT_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.STORYBOARD_READY,
]
TO_RENDER_READY = [
    *TO_STORYBOARD_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.ASSETS_READY,
    EpisodeEvent.STAGE_ADMITTED,
    EpisodeEvent.RENDER_READY,
]


async def _slot_count(factory) -> int:
    async with factory() as session:
        return int(
            (await session.scalar(select(func.count()).select_from(DailyEpisodeSlotRow))) or 0
        )


async def _status(factory, episode_id: str) -> EpisodeStatus:
    async with factory() as session:
        episode = await EpisodeRepository(session).get(episode_id)
        assert episode is not None
        return episode.status


# ---------------------------------------------------------------------------------- (a)


async def test_get_plan_dry_run_makes_no_writes_and_no_starter_calls(stack: Stack) -> None:
    episode_id = await _episode_at(stack.factory, TO_STORYBOARD_READY)
    before = await _status(stack.factory, episode_id)
    slots_before = await _slot_count(stack.factory)

    async with await stack.app_client() as api:
        response = await api.get(f"/episodes/{episode_id}/resume/plan")
        # 2回目（違う Episode）も含め、GET は何回呼んでも starter に触れない
        await api.get(f"/episodes/{uuid.uuid4()}/resume/plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resumable"] is True
    assert body["target_stage"] == "production"
    assert body["stages_to_run"] == ["production", "render", "upload"]

    after = await _status(stack.factory, episode_id)
    assert after is before  # 状態遷移すら起きていない（IdentityでOK: Enumメンバ）
    assert await _slot_count(stack.factory) == slots_before == 0
    # QueueScopedStarter の全メソッドは呼ばれたら AssertionError。ここまで到達した時点で
    # 一度も呼ばれていないことの証拠（例外が飛んでいない）


# ---------------------------------------------------------------------------------- (b)(d)


async def test_resume_reaches_upload_after_blocked_production_without_redoing_finished_scenes(
    stack: Stack,
) -> None:
    episode_id = await _episode_at(stack.factory, TO_STORYBOARD_READY)

    async with stack.worker():
        async with await stack.app_client() as api:
            first = await api.post(f"/episodes/{episode_id}/resume")
            assert first.status_code == 202, first.text
            assert first.json()["target_stage"] == "production"
        first_result = await stack.pipeline_result(episode_id)
        assert first_result["outcome"] == "stopped"
        assert first_result["stopped_stage"] == "production"
        assert await _status(stack.factory, episode_id) is EpisodeStatus.BLOCKED
        assert stack.world.production_attempts == 1
        assert stack.world.image_submit_calls == 3

        async with await stack.app_client() as api:
            plan = await api.get(f"/episodes/{episode_id}/resume/plan")
            assert plan.status_code == 200, plan.text
            assert plan.json()["resumable"] is True
            assert plan.json()["target_stage"] == "production"

            second = await api.post(f"/episodes/{episode_id}/resume")
            assert second.status_code == 202, second.text
        second_result = await stack.pipeline_result(episode_id)

    assert second_result["outcome"] == "completed"
    assert second_result["status"] == EpisodeStatus.UPLOADED.value
    assert await _status(stack.factory, episode_id) is EpisodeStatus.UPLOADED
    # production は2回呼ばれたが、生成呼び出しは 3 + 1 == 4（完了済みの3シーンは作り直さない）
    assert stack.world.production_attempts == 2
    assert stack.world.image_submit_calls == 4
    assert stack.world.render_calls == 1
    assert stack.world.upload_calls == 1
    # (d) resume は日次枠を消費しない
    assert await _slot_count(stack.factory) == 0


# ---------------------------------------------------------------------------------- (c)


async def test_two_concurrent_resumes_start_exactly_one_pipeline_execution(stack: Stack) -> None:
    episode_id = await _episode_at(stack.factory, TO_STORYBOARD_READY)

    async with stack.worker():
        async with await stack.app_client() as api:
            first, second = await asyncio.gather(
                api.post(f"/episodes/{episode_id}/resume"),
                api.post(f"/episodes/{episode_id}/resume"),
            )
        codes = sorted([first.status_code, second.status_code])
        assert codes == [202, 409], (first.text, second.text)

        result = await stack.pipeline_result(episode_id)
        assert result["outcome"] == "stopped"
        assert result["stopped_stage"] == "production"

    # 実行されたのは1本だけ（fake production は blocked になるところまでで1回しか呼ばれない）
    assert stack.world.production_attempts == 1


# ---------------------------------------------------------------------------------- (e)


async def test_resume_does_not_bypass_uploads_paused(stack: Stack) -> None:
    episode_id = await _episode_at(stack.factory, TO_RENDER_READY)
    async with stack.factory() as session:
        await ArtifactMetadataRepository(session).record(
            episode_id=episode_id,
            artifact_type=ArtifactType.FINAL_VIDEO,
            schema_version="resume-it-fake",
            bucket="artifacts",
            object_key=f"resume-it/{episode_id}/final.mp4",
            sha256="f" * 64,
        )
        await OperationalSwitchRepository(session).set(OperationalSwitch.UPLOADS_PAUSED, True)
        await session.commit()

    async with stack.worker():
        async with await stack.app_client() as api:
            response = await api.post(f"/episodes/{episode_id}/resume")
            assert response.status_code == 202, response.text
            assert response.json()["target_stage"] == "upload"
        result = await stack.pipeline_result(episode_id)

    assert result["outcome"] == "upload_skipped"
    assert result["stopped_stage"] == "upload"
    assert "uploads_paused" in (result["reason"] or "")
    assert stack.world.upload_calls == 0
    assert await _status(stack.factory, episode_id) is EpisodeStatus.RENDER_READY
