"""失敗した production を POST（同じ workflow id の再起動）で再開する縦切り（ADR-0017 §8）。

本物の Temporal + PostgreSQL + MinIO。状態系・メディア Activity は本物、生成器だけ fake（INV-18）。
1回目は音声が needs_input で止まり ``blocked``。2回目は ``RESUMED`` で ``assets_ready`` へ。
画像・動画の有料 submit はシーンごとに**全実行を通して1回**（再利用 / 台帳からの await 再開）。
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from temporalio.client import Client
from temporalio.worker import Worker

from contracts.states import EpisodeStatus
from infrastructure.media.probe import PillowAvMediaProbe
from infrastructure.production.paid_job import PaidJobRunner
from infrastructure.workdir import WorkDirectory
from tests.integration.test_production_activities import (  # noqa: F401
    _seed,
    _status,
    factory,
    store,
)
from tests.support.db import require_test_database_url
from tests.support.production import FakeImageGenerator, FakeVideoGenerator, FakeVoiceGenerator
from tests.support.storyboard import BUCKET
from workers.production.activities import ProductionActivities
from workers.production.run_inspector import TemporalWorkflowRunInspector
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput
from workers.production_image.activities import ImageProductionActivities
from workers.production_video.activities import VideoProductionActivities
from workers.production_voice.activities import VoiceActivities

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")
TEST_DATABASE_URL = require_test_database_url()

pytestmark = pytest.mark.skipif(
    not TEMPORAL_ADDRESS or not TEST_DATABASE_URL or not os.environ.get("MINIO_ENDPOINT"),
    reason="TEMPORAL_ADDRESS, TEST_DATABASE_URL (*_test) and MINIO_ENDPOINT must be set",
)


class ToggleVoice(FakeVoiceGenerator):
    def __init__(self) -> None:
        super().__init__()
        self.broken = True

    async def synthesize(self, text, language, dest) -> None:
        if self.broken:
            self.calls += 1
            raise RuntimeError("fake: voice model missing")
        await super().synthesize(text, language, dest)


async def test_failed_production_resumes_on_post_without_new_paid_submits(
    factory,  # noqa: F811
    store,  # noqa: F811
    tmp_path,
) -> None:
    seeded = await _seed(factory, store)
    client = await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")
    suffix = uuid.uuid4().hex[:10]
    queues = {k: f"production-{k}-rerun-{suffix}" for k in ("state", "image", "video", "voice")}
    image_gen = FakeImageGenerator(pending_polls=1)
    video_gen = FakeVideoGenerator(pending_polls=1)
    voice_gen = ToggleVoice()

    def runner(sub: str) -> PaidJobRunner:
        return PaidJobRunner(
            session_factory=factory,
            store=store,
            workdir=WorkDirectory(tmp_path / sub, forbidden=()),
        )

    workers = [
        Worker(
            client,
            task_queue=queues["state"],
            workflows=[ProductionWorkflow],
            activities=ProductionActivities(
                session_factory=factory,
                store=store,
                bucket=BUCKET,
                run_inspector=TemporalWorkflowRunInspector(client),
            ).all_activities(),
        ),
        Worker(
            client,
            task_queue=queues["image"],
            activities=ImageProductionActivities(
                session_factory=factory,
                store=store,
                generator=image_gen,
                probe=PillowAvMediaProbe(),
                runner=runner("image"),
                bucket=store.bucket,
                poll_interval_seconds=0,
            ).all_activities(),
        ),
        Worker(
            client,
            task_queue=queues["video"],
            activities=VideoProductionActivities(
                session_factory=factory,
                store=store,
                generator=video_gen,
                probe=PillowAvMediaProbe(),
                runner=runner("video"),
                bucket=store.bucket,
                poll_interval_seconds=0,
            ).all_activities(),
        ),
        Worker(
            client,
            task_queue=queues["voice"],
            activities=VoiceActivities(
                session_factory=factory,
                store=store,
                generator=voice_gen,
                probe=PillowAvMediaProbe(),
                workdir=WorkDirectory(tmp_path / "voice", forbidden=()),
                bucket=store.bucket,
            ).all_activities(),
        ),
    ]
    workflow_id = f"episode-{seeded.episode_id}-production-{suffix}"
    request = ProductionWorkflowInput(
        episode_id=seeded.episode_id,
        image_task_queue=queues["image"],
        video_task_queue=queues["video"],
        voice_task_queue=queues["voice"],
    )

    async def post():
        return await asyncio.wait_for(
            client.execute_workflow(
                ProductionWorkflow.run, request, id=workflow_id, task_queue=queues["state"]
            ),
            timeout=180,
        )

    for w in workers:
        await w.__aenter__()
    try:
        first = await post()
        assert first.status == EpisodeStatus.BLOCKED.value, first
        assert await _status(factory, seeded.episode_id) is EpisodeStatus.BLOCKED

        voice_gen.broken = False
        second = await post()
    finally:
        for w in reversed(workers):
            await w.__aexit__(None, None, None)

    assert second.admitted and second.status == EpisodeStatus.ASSETS_READY.value, second
    assert await _status(factory, seeded.episode_id) is EpisodeStatus.ASSETS_READY
    assert second.manifest is not None
    # 1回目に submit 済みのシーンは再 submit しない（Artifact 再利用 / 台帳から await 再開）
    assert image_gen.submit_calls == 4
    assert video_gen.submit_calls == 4
