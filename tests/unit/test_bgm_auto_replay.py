"""品質 gate を投稿 Activity の中に足し flag を足しても、workflow の決定性は崩れない（INV-158）。

workflow のコードは変えていない。変えたのは Activity の中身と、返しうる失敗の型
（``UploadQualityRefusedError``）だけ。次の 2 つで確かめる:

1. 既存の履歴（変更前のコードで採った ``tests/unit/fixtures``）が、今のコードで replay できる。
2. 新しい失敗型で終わった UploadWorkflow の履歴を time-skipping 環境で作り、replay できる。
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

from contracts.upload_activities import (
    UPLOAD_ADMIT,
    UPLOAD_AWAIT_PROCESSING,
    UPLOAD_FINAL_VIDEO,
    UPLOAD_MARK_UPLOADED,
    UPLOAD_RECORD_FAILURE,
)
from tests.unit.test_upload_workflow import Mocks, _error
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow
from workers.render.workflows import RenderWorkflow
from workers.upload.workflows import UploadWorkflow, UploadWorkflowInput

FIXTURES = Path(__file__).parent / "fixtures"
RECORDED = [
    *sorted((FIXTURES / "pre_logging").glob("upload_*.json")),
    *sorted((FIXTURES / "pre_logging").glob("pipeline_*.json")),
    FIXTURES / "episode_pipeline_pre_0058_history.json",
    FIXTURES / "daily_episode_pre_0058_history.json",
]
WORKFLOWS = [DailyEpisodeWorkflow, EpisodePipelineWorkflow, RenderWorkflow, UploadWorkflow]


def test_the_recorded_histories_include_the_upload_and_the_gate_refusal_paths() -> None:
    names = {p.stem for p in RECORDED}
    assert {"upload_succeeded", "upload_blocked", "pipeline_upload_gate_refused"} <= names


@pytest.mark.parametrize("path", RECORDED, ids=lambda p: p.stem)
async def test_recorded_histories_replay_against_the_current_workflows(path: Path) -> None:
    history = WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))
    await Replayer(workflows=WORKFLOWS).replay_workflow(history)


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


async def _history_of(env: WorkflowEnvironment, mocks: Mocks) -> WorkflowHistory:
    queue = f"replay-{uuid.uuid4().hex[:10]}"
    media = f"replay-media-{uuid.uuid4().hex[:10]}"
    acts = mocks.activities()
    state = {UPLOAD_ADMIT, UPLOAD_AWAIT_PROCESSING, UPLOAD_MARK_UPLOADED, UPLOAD_RECORD_FAILURE}
    async with (
        Worker(
            env.client,
            task_queue=queue,
            workflows=[UploadWorkflow],
            activities=[a for a in acts if a.__temporal_activity_definition.name in state],
        ),
        Worker(
            env.client,
            task_queue=media,
            activities=[
                a for a in acts if a.__temporal_activity_definition.name == UPLOAD_FINAL_VIDEO
            ],
            max_heartbeat_throttle_interval=timedelta(milliseconds=50),
            default_heartbeat_throttle_interval=timedelta(milliseconds=50),
        ),
    ):
        handle = await env.client.start_workflow(
            UploadWorkflow.run,
            UploadWorkflowInput(episode_id="ep-1", upload_task_queue=media),
            id=f"episode-ep-1-upload-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
        )
        await handle.result()
        return await handle.fetch_history()


async def test_a_quality_refusal_ends_the_upload_workflow_without_retry_and_replays(
    env: WorkflowEnvironment,
) -> None:
    mocks = Mocks(always_fail=_error("UploadQualityRefusedError", non_retryable=True))
    history = await _history_of(env, mocks)
    assert mocks.calls == ["admit", "upload", "record_failure"]  # retry しない
    (failure,) = mocks.failures
    assert failure.failure_class == "needs_input" and not failure.retry_exhausted
    await Replayer(workflows=[UploadWorkflow]).replay_workflow(history)


async def test_a_normal_upload_workflow_history_replays(env: WorkflowEnvironment) -> None:
    history = await _history_of(env, Mocks())
    await Replayer(workflows=[UploadWorkflow]).replay_workflow(history)
