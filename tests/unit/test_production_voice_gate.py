"""ProductionWorkflow は、有料の画像・動画より前に音声が区間へ収まることを確かめる（ADR-0028）。

音声（ローカル・非課金）は合成の実尺が分かった時点で区間との適合を判定する。溢れる音声で
有料の制作を回してから描画で落とさないよう、音声の枝が全部成功するまで
画像・動画を起動しない。time-skipping テストサーバ + 名前で登録した mock Activity。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest_asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.production_activities import (
    IMAGE_AWAIT,
    IMAGE_SUBMIT,
    PRODUCTION_ADMIT,
    PRODUCTION_ASSEMBLE_MANIFEST,
    PRODUCTION_MARK_READY,
    PRODUCTION_PLAN,
    PRODUCTION_RECORD_FAILURE,
    VIDEO_AWAIT,
    VIDEO_SUBMIT,
    VOICE_GENERATE,
    ImageAwaitRequest,
    ImageSubmitRequest,
    ProductionAdmitRequest,
    ProductionAdmitResult,
    ProductionAssembleRequest,
    ProductionFailureOutcome,
    ProductionMarkReadyRequest,
    ProductionMarkReadyResult,
    ProductionPlan,
    ProductionPlanRequest,
    ProductionRecordFailureRequest,
    SceneArtifactResult,
    SceneImageWork,
    SceneVideoWork,
    SceneVoiceWork,
    SubmitResult,
    VideoAwaitRequest,
    VideoSubmitRequest,
    VoiceGenerateRequest,
)
from contracts.states import EpisodeStatus
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput

SCENES = ["sb1", "sb2"]
VOICES = ["s1", "s2"]


def _artifact(label: str) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=str(uuid.uuid5(uuid.NAMESPACE_OID, label)),
        object_key=f"k/{label}",
        sha256="a" * 64,
        reused=False,
    )


@dataclass
class Recorder:
    events: list[str] = field(default_factory=list)
    failures: list[ProductionRecordFailureRequest] = field(default_factory=list)
    failing_voice: str | None = None

    def state(self) -> list[Callable[..., Any]]:
        @activity.defn(name=PRODUCTION_ADMIT)
        async def admit(req: ProductionAdmitRequest) -> ProductionAdmitResult:
            return ProductionAdmitResult(admitted=True, status="in_progress")

        @activity.defn(name=PRODUCTION_PLAN)
        async def plan(req: ProductionPlanRequest) -> ProductionPlan:
            return ProductionPlan(
                storyboard_artifact_id="sb-art",
                storyboard_sha256="b" * 64,
                script_artifact_id="sc-art",
                script_sha256="c" * 64,
                images=[SceneImageWork(scene_id=s) for s in SCENES],
                videos=[SceneVideoWork(scene_id=s, requested_duration_ms=4000) for s in SCENES],
                voices=[
                    SceneVoiceWork(script_scene_id=v, storyboard_scene_ids=["sb1"]) for v in VOICES
                ],
            )

        @activity.defn(name=PRODUCTION_ASSEMBLE_MANIFEST)
        async def assemble(req: ProductionAssembleRequest) -> SceneArtifactResult:
            self.events.append("assemble")
            return _artifact("manifest")

        @activity.defn(name=PRODUCTION_MARK_READY)
        async def mark_ready(req: ProductionMarkReadyRequest) -> ProductionMarkReadyResult:
            return ProductionMarkReadyResult(status=EpisodeStatus.ASSETS_READY.value)

        @activity.defn(name=PRODUCTION_RECORD_FAILURE)
        async def record_failure(req: ProductionRecordFailureRequest) -> ProductionFailureOutcome:
            self.failures.append(req)
            return ProductionFailureOutcome(episode_status="blocked")

        return [admit, plan, assemble, mark_ready, record_failure]

    def image(self) -> list[Callable[..., Any]]:
        @activity.defn(name=IMAGE_SUBMIT)
        async def submit(req: ImageSubmitRequest) -> SubmitResult:
            self.events.append(f"image_submit:{req.scene_id}")
            return SubmitResult(reservation_id=f"r-{req.scene_id}")

        @activity.defn(name=IMAGE_AWAIT)
        async def wait(req: ImageAwaitRequest) -> SceneArtifactResult:
            return _artifact(f"img-{req.scene_id}")

        return [submit, wait]

    def video(self) -> list[Callable[..., Any]]:
        @activity.defn(name=VIDEO_SUBMIT)
        async def submit(req: VideoSubmitRequest) -> SubmitResult:
            self.events.append(f"video_submit:{req.scene_id}")
            return SubmitResult(reservation_id=f"v-{req.scene_id}")

        @activity.defn(name=VIDEO_AWAIT)
        async def wait(req: VideoAwaitRequest) -> SceneArtifactResult:
            return _artifact(f"vid-{req.scene_id}")

        return [submit, wait]

    def voice(self) -> list[Callable[..., Any]]:
        @activity.defn(name=VOICE_GENERATE)
        async def generate(req: VoiceGenerateRequest) -> SceneArtifactResult:
            self.events.append(f"voice:{req.script_scene_id}")
            if req.script_scene_id == self.failing_voice:
                raise ApplicationError(
                    "voice 10147 ms > span 7000 ms",
                    type="VoiceExceedsSceneSpanError",
                    non_retryable=True,
                )
            return _artifact(f"voice-{req.script_scene_id}")

        return [generate]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


async def _run(env: WorkflowEnvironment, recorder: Recorder):
    suffix = uuid.uuid4().hex[:10]
    queues = {name: f"{name}-{suffix}" for name in ("production", "image", "video", "voice")}
    workers = [
        Worker(
            env.client,
            task_queue=queues["production"],
            workflows=[ProductionWorkflow],
            activities=recorder.state(),
        ),
        Worker(env.client, task_queue=queues["image"], activities=recorder.image()),
        Worker(env.client, task_queue=queues["video"], activities=recorder.video()),
        Worker(env.client, task_queue=queues["voice"], activities=recorder.voice()),
    ]
    for worker in workers:
        await worker.__aenter__()
    try:
        return await env.client.execute_workflow(
            ProductionWorkflow.run,
            ProductionWorkflowInput(
                episode_id="ep-1",
                image_task_queue=queues["image"],
                video_task_queue=queues["video"],
                voice_task_queue=queues["voice"],
            ),
            id=f"episode-ep-1-production-{suffix}",
            task_queue=queues["production"],
        )
    finally:
        for worker in reversed(workers):
            await worker.__aexit__(None, None, None)


async def test_voice_that_cannot_fit_stops_production_before_any_paid_media(env) -> None:
    """9/19 型: 溢れる音声があるとき、画像・動画（有料）を1件も起動しない。"""
    recorder = Recorder(failing_voice="s1")

    result = await _run(env, recorder)

    assert not [e for e in recorder.events if e.startswith(("image_", "video_"))]
    assert "assemble" not in recorder.events
    [failure] = recorder.failures
    assert failure.failure_class == "needs_input"
    assert result.status != EpisodeStatus.ASSETS_READY.value


async def test_media_starts_only_after_every_voice_succeeded(env) -> None:
    recorder = Recorder()

    result = await _run(env, recorder)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    first_media = next(i for i, e in enumerate(recorder.events) if e.startswith("image_"))
    voices_done = [i for i, e in enumerate(recorder.events) if e.startswith("voice:")]
    assert len(voices_done) == len(VOICES) and max(voices_done) < first_media
