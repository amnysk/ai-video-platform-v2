"""残高不足は workflow を終わらせる管理された一時停止（ADR-0057 / INV-73）。

time-skipping テストサーバ + 実際の ``raise_activity_error`` で作った ``ApplicationError`` を
返す mock Activity。見るのは Temporal 側の振る舞い: 残高拒否は

- Activity の retry 嵐を起こさない（1 submit = 1 attempt。non_retryable）
- workflow を ``blocked`` + ``needs_input`` で**終わらせる**（待ち続けない）
- 7日経っても履歴が伸びない・再び動かない
- 他の工程（音声など、別 provider）を止めない・成功済みのシーンの結果を捨てない

breaker・保留の DB 側は ``test_paid_job_balance.py`` が見る。Workflow コードは変えていない。
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest_asyncio
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.production_activities import (
    IMAGE_AWAIT,
    IMAGE_SUBMIT,
    PLAN_SCENE_ALTERNATIVE,
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
    PlanSceneAlternativeRequest,
    ProductionAdmitRequest,
    ProductionAdmitResult,
    ProductionAssembleRequest,
    ProductionFailureOutcome,
    ProductionMarkReadyRequest,
    ProductionMarkReadyResult,
    ProductionPlan,
    ProductionPlanRequest,
    ProductionRecordFailureRequest,
    SceneAlternativeOutcome,
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
from domain.errors import ProviderBalanceExhaustedError, ProviderBreakerOpenError
from infrastructure.production.activity_errors import raise_activity_error
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput

BALANCE = ProviderBalanceExhaustedError("fal submit refused: HTTP 403 balance exhausted")
OPEN = ProviderBreakerOpenError("fal:default: provider breaker is open (balance_exhausted)")


def _artifact(label: str) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=str(uuid.uuid5(uuid.NAMESPACE_OID, label)),
        object_key=f"k/{label}",
        sha256="a" * 64,
        reused=False,
    )


@dataclass
class Recorder:
    scenes: list[str]
    #: image submit がこの例外で拒否されるシーン
    refuse: dict[str, Exception] = field(default_factory=dict)
    image_attempts: Counter[str] = field(default_factory=Counter)
    video_submits: Counter[str] = field(default_factory=Counter)
    voice_calls: int = 0
    failures: list[ProductionRecordFailureRequest] = field(default_factory=list)

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
                images=[SceneImageWork(scene_id=s) for s in self.scenes],
                videos=[
                    SceneVideoWork(scene_id=s, requested_duration_ms=4000) for s in self.scenes
                ],
                voices=[SceneVoiceWork(script_scene_id="s1", storyboard_scene_ids=self.scenes)],
            )

        @activity.defn(name=PRODUCTION_ASSEMBLE_MANIFEST)
        async def assemble(req: ProductionAssembleRequest) -> SceneArtifactResult:
            return _artifact("manifest")

        @activity.defn(name=PRODUCTION_MARK_READY)
        async def mark_ready(req: ProductionMarkReadyRequest) -> ProductionMarkReadyResult:
            return ProductionMarkReadyResult(status=EpisodeStatus.ASSETS_READY.value)

        @activity.defn(name=PRODUCTION_RECORD_FAILURE)
        async def record_failure(req: ProductionRecordFailureRequest) -> ProductionFailureOutcome:
            self.failures.append(req)
            return ProductionFailureOutcome(episode_status="blocked")

        return [admit, plan, assemble, mark_ready, record_failure]

    def media(self) -> list[Callable[..., Any]]:
        @activity.defn(name=IMAGE_SUBMIT)
        async def image_submit(req: ImageSubmitRequest) -> SubmitResult:
            self.image_attempts[req.scene_id] += 1
            error = self.refuse.get(req.scene_id)
            if error is not None:
                try:
                    raise error
                except Exception as exc:  # 本物の Activity 境界と同じ写像
                    raise_activity_error(exc)
            return SubmitResult(reservation_id=f"ri-{req.scene_id}")

        @activity.defn(name=IMAGE_AWAIT)
        async def image_await(req: ImageAwaitRequest) -> SceneArtifactResult:
            return _artifact(f"img-{req.scene_id}")

        @activity.defn(name=VIDEO_SUBMIT)
        async def video_submit(req: VideoSubmitRequest) -> SubmitResult:
            self.video_submits[req.scene_id] += 1
            return SubmitResult(reservation_id=f"rv-{req.scene_id}")

        @activity.defn(name=VIDEO_AWAIT)
        async def video_await(req: VideoAwaitRequest) -> SceneArtifactResult:
            return _artifact(f"vid-{req.scene_id}")

        @activity.defn(name=VOICE_GENERATE)
        async def voice(req: VoiceGenerateRequest) -> SceneArtifactResult:
            self.voice_calls += 1
            return _artifact("voice")

        return [image_submit, image_await, video_submit, video_await, voice]

    def planner(self) -> list[Callable[..., Any]]:
        @activity.defn(name=PLAN_SCENE_ALTERNATIVE)
        async def plan(req: PlanSceneAlternativeRequest) -> SceneAlternativeOutcome:
            raise AssertionError("a balance refusal must never ask for a scene alternative")

        return [plan]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


async def _start(env: WorkflowEnvironment, recorder: Recorder):
    """workflow を走らせ ``(result, handle)`` を返す。worker は結果が出た後に畳む。"""
    suffix = uuid.uuid4().hex[:10]
    queues = {n: f"{n}-{suffix}" for n in ("production", "media", "planner")}
    workers = [
        Worker(
            env.client,
            task_queue=queues["production"],
            workflows=[ProductionWorkflow],
            activities=recorder.state(),
        ),
        Worker(env.client, task_queue=queues["media"], activities=recorder.media()),
        Worker(env.client, task_queue=queues["planner"], activities=recorder.planner()),
    ]
    for worker in workers:
        await worker.__aenter__()
    try:
        handle = await env.client.start_workflow(
            ProductionWorkflow.run,
            ProductionWorkflowInput(
                episode_id="ep-1",
                image_task_queue=queues["media"],
                video_task_queue=queues["media"],
                voice_task_queue=queues["media"],
                scene_alternative_task_queue=queues["planner"],
            ),
            id=f"episode-ep-1-production-{suffix}",
            task_queue=queues["production"],
        )
        result = await handle.result()
        return result, handle
    finally:
        for worker in reversed(workers):
            await worker.__aexit__(None, None, None)


async def _history_len(handle) -> int:
    return len((await handle.fetch_history()).events)


async def test_balance_refusal_ends_the_workflow_blocked_without_a_retry_storm(env) -> None:
    recorder = Recorder(scenes=["sb1"], refuse={"sb1": BALANCE})

    result, handle = await _start(env, recorder)

    assert result.status == "blocked" and result.failure_class == "needs_input"
    assert (
        recorder.image_attempts["sb1"] == 1
    )  # non_retryable: Activity の retry も workflow の再 submit も無い
    [failure] = recorder.failures
    assert failure.failure_class == "needs_input"
    assert "ProviderBalanceExhaustedError" in failure.error_summary


async def test_an_open_breaker_ends_the_workflow_the_same_way(env) -> None:
    recorder = Recorder(scenes=["sb1"], refuse={"sb1": OPEN})

    result, _ = await _start(env, recorder)

    assert result.status == "blocked"
    assert recorder.image_attempts["sb1"] == 1
    assert "ProviderBreakerOpenError" in recorder.failures[0].error_summary


async def test_a_seven_day_hold_costs_no_more_activity_and_no_more_history(env) -> None:
    """保留は workflow を終わらせる: 7日待っても再 submit せず、履歴は伸びない。"""
    recorder = Recorder(scenes=["sb1"], refuse={"sb1": BALANCE})

    _, handle = await _start(env, recorder)
    before_events = await _history_len(handle)
    before_attempts = dict(recorder.image_attempts)

    await env.sleep(timedelta(days=7))

    assert await _history_len(handle) == before_events  # 履歴は終了時のまま
    assert before_events < 80  # 有界（1 scene の拒否で終わる履歴）
    assert dict(recorder.image_attempts) == before_attempts  # 7日間 1回も再 submit していない
    assert len(recorder.failures) == 1
    description = await handle.describe()
    assert description.status is not None
    assert description.status.name == "COMPLETED"  # 待機中ではなく終了している
