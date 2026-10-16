"""ProductionWorkflow は provider に拒否されたシーンだけを代替案で作り直す（ADR-0035）。

time-skipping テストサーバ + 名前で登録した mock Activity（``test_production_voice_gate.py`` と
同じ型）。有料の submit 回数をシーンごとに数え、拒否されていないシーンが増えないことを見る。
"""

from __future__ import annotations

import uuid
from collections import Counter
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
    MAX_SCENE_ALTERNATIVES_PER_SCENE,
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
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput

SCENES = ["sb1", "sb2", "sb3"]


def _artifact(label: str) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=str(uuid.uuid5(uuid.NAMESPACE_OID, label)),
        object_key=f"k/{label}",
        sha256="a" * 64,
        reused=False,
    )


@dataclass
class Recorder:
    #: 拒否するシーン → この revision 未満の案なら動画で拒否する
    reject_until_revision: dict[str, int] = field(default_factory=dict)
    reject_type: str = "ProviderRejectedError"
    planner_error: str | None = None
    image_submits: Counter[str] = field(default_factory=Counter)
    video_submits: Counter[str] = field(default_factory=Counter)
    plans: list[PlanSceneAlternativeRequest] = field(default_factory=list)
    failures: list[ProductionRecordFailureRequest] = field(default_factory=list)
    revision: dict[str, int] = field(default_factory=dict)
    #: 止まる系の試験は1シーンにする（兄弟の cancel は mock Activity が受け取れず、
    #: WAIT_CANCELLATION_COMPLETED の試験サーバで終わらない ── 復旧の検査と無関係な待ち）
    scenes: list[str] = field(default_factory=lambda: list(SCENES))

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
            self.image_submits[req.scene_id] += 1
            return SubmitResult(reservation_id=f"ri-{req.scene_id}")

        @activity.defn(name=IMAGE_AWAIT)
        async def image_await(req: ImageAwaitRequest) -> SceneArtifactResult:
            return _artifact(f"img-{req.scene_id}-{self.revision.get(req.scene_id, 0)}")

        @activity.defn(name=VIDEO_SUBMIT)
        async def video_submit(req: VideoSubmitRequest) -> SubmitResult:
            self.video_submits[req.scene_id] += 1
            return SubmitResult(reservation_id=f"rv-{req.scene_id}")

        @activity.defn(name=VIDEO_AWAIT)
        async def video_await(req: VideoAwaitRequest) -> SceneArtifactResult:
            if self.revision.get(req.scene_id, 0) < self.reject_until_revision.get(req.scene_id, 0):
                raise ApplicationError(
                    "fal job failed: HTTP 422 content_policy_violation (at body.image_url)",
                    type=self.reject_type,
                    non_retryable=True,
                )
            return _artifact(f"vid-{req.scene_id}")

        @activity.defn(name=VOICE_GENERATE)
        async def voice(req: VoiceGenerateRequest) -> SceneArtifactResult:
            return _artifact("voice")

        return [image_submit, image_await, video_submit, video_await, voice]

    def planner(self) -> list[Callable[..., Any]]:
        @activity.defn(name=PLAN_SCENE_ALTERNATIVE)
        async def plan(req: PlanSceneAlternativeRequest) -> SceneAlternativeOutcome:
            self.plans.append(req)
            if self.planner_error:
                raise ApplicationError(
                    "this scene already had 2 automatic alternative(s)",
                    type=self.planner_error,
                    non_retryable=True,
                )
            self.revision[req.scene_id] = self.revision.get(req.scene_id, 0) + 1
            return SceneAlternativeOutcome(
                override_artifact_id=f"ov-{req.scene_id}-{self.revision[req.scene_id]}",
                revision=self.revision[req.scene_id],
                visual_subject="landscape",
                newly_planned=True,
            )

        return [plan]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


async def _run(env: WorkflowEnvironment, recorder: Recorder):
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
        return await env.client.execute_workflow(
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
    finally:
        for worker in reversed(workers):
            await worker.__aexit__(None, None, None)


async def test_only_the_rejected_scene_is_rebuilt_from_its_image(env) -> None:
    """2026-09-26/27 型: sb2 の動画だけ拒否 → sb2 だけ代替案 → sb2 の画像から作り直して完成。"""
    recorder = Recorder(reject_until_revision={"sb2": 1})

    result = await _run(env, recorder)

    assert result.status == EpisodeStatus.ASSETS_READY.value, recorder.failures
    assert [(p.scene_id, p.seen_revision) for p in recorder.plans] == [("sb2", 0)]
    assert recorder.image_submits == Counter({"sb1": 1, "sb2": 2, "sb3": 1})
    assert recorder.video_submits == Counter({"sb1": 1, "sb2": 2, "sb3": 1})
    assert recorder.failures == []


async def test_retry_blocked_on_resume_also_plans_an_alternative(env) -> None:
    """旧 Episode の resume: 拒否済み入力の再送禁止で止まったシーンも同じ復旧に入る。"""
    recorder = Recorder(
        reject_until_revision={"sb2": 1}, reject_type="ProviderRejectedRetryBlockedError"
    )

    result = await _run(env, recorder)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert [p.scene_id for p in recorder.plans] == ["sb2"]


async def test_planner_limit_stops_with_needs_input_and_the_reason(env) -> None:
    recorder = Recorder(
        reject_until_revision={"sb2": 1},
        planner_error="SceneAlternativeLimitReachedError",
        scenes=["sb2"],
    )

    result = await _run(env, recorder)

    assert result.status == "blocked"
    [failure] = recorder.failures
    assert failure.failure_class == "needs_input"
    assert "sb2" in failure.error_summary and "alternative" in failure.error_summary
    assert recorder.image_submits["sb2"] == 1  # 作り直していない


async def test_workflow_never_asks_the_planner_more_than_the_scene_limit(env) -> None:
    """Activity 側の判定が壊れていても、1実行で planner を上限回数より多く呼ばない。"""
    recorder = Recorder(reject_until_revision={"sb2": 99}, scenes=["sb2"])

    result = await _run(env, recorder)

    assert result.status == "blocked"
    assert len(recorder.plans) == MAX_SCENE_ALTERNATIVES_PER_SCENE
    [failure] = recorder.failures
    assert "still rejected" in failure.error_summary


async def test_other_failures_do_not_trigger_alternative_planning(env) -> None:
    """認可拒否（403）は内容の問題ではない。代替案を作らない（ADR-0030 の経路のまま）。"""
    recorder = Recorder(
        reject_until_revision={"sb2": 1}, reject_type="ProviderUnavailableError", scenes=["sb2"]
    )

    result = await _run(env, recorder)

    assert result.status == "blocked"
    assert recorder.plans == []
