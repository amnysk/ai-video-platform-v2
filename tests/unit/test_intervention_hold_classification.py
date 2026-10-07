"""dispatch gate の拒否（``EpisodeHeldError`` / ``MissingDecisionRecordError``）の失敗分類。

ADR-0041 §2・INV-44/46。gate そのもの（``mark_dispatched`` の中の検査）は担当 B の実装なので、
ここでは**gate を stub する**（``ProviderReservationRepository.mark_dispatched`` を差し替えて
例外を投げさせる。repositories.py は編集しない）。確かめるのは、gate が投げた後の既存経路が

- needs_input → Episode は blocked（failed ではない）
- Activity も workflow のラウンドも再試行しない（retry loop にならない）
- 照合待ち（unreconciled / 成否不明の submit）に化けない。予約は reserved・未 dispatch のまま残り、
  再開後に同じ予約で送る（新しい予約・二重課金を作らない）

になること。理由は docs/testing/intervention-d-rationale.md §4。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
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
from contracts.states import ArtifactType, FailureClass, ProviderCall, ReservationStatus
from domain.errors import (
    NON_RETRYABLE_ERROR_TYPE_NAMES,
    EpisodeHeldError,
    MissingDecisionRecordError,
    UnreconciledReservationError,
    classify_failure,
    failure_class_from_type_name,
)
from domain.production.ports import ImageRequest
from infrastructure.db.repositories import EpisodeRepository, ProviderReservationRepository
from infrastructure.logging.provider import classify
from infrastructure.production.activity_errors import raise_activity_error
from infrastructure.production.paid_job import PaidJobRunner, PaidJobSpec, Submitted
from infrastructure.workdir import WorkDirectory
from tests.support.production import FakeImageGenerator
from workers.production import workflows as production_workflows
from workers.production.workflows import ProductionWorkflow, ProductionWorkflowInput

GATE_ERRORS = (EpisodeHeldError, MissingDecisionRecordError)


# --------------------------------------------------------------------------- 分類


@pytest.mark.parametrize("error", GATE_ERRORS)
def test_gate_errors_are_needs_input_and_never_retried(error: type[Exception]) -> None:
    assert classify_failure(error("held")) is FailureClass.NEEDS_INPUT
    assert failure_class_from_type_name(error.__name__) is FailureClass.NEEDS_INPUT
    assert error.__name__ in NON_RETRYABLE_ERROR_TYPE_NAMES


@pytest.mark.parametrize("error", GATE_ERRORS)
def test_gate_errors_are_not_reconciliation_or_round_errors(error: type[Exception]) -> None:
    """待ち直し・新ラウンド・代替映像案・照合待ちのどれにも入らない。"""
    name = error.__name__
    assert not issubclass(error, UnreconciledReservationError)
    assert name not in production_workflows.REAWAIT_ERROR_TYPE_NAMES
    assert name not in production_workflows.NEW_ROUND_ERROR_TYPE_NAMES
    assert name not in production_workflows.SCENE_REJECTION_ERROR_TYPE_NAMES
    assert name not in production_workflows.INPUT_FETCH_ERROR_TYPE_NAMES
    category, _ = classify(error("held"), None)
    assert category not in {"unreconciled_reservation", "submit_ambiguous"}


@pytest.mark.parametrize("error", GATE_ERRORS)
def test_activity_boundary_keeps_the_type_and_stops_retries(error: type[Exception]) -> None:
    try:
        raise error("episode is held")
    except Exception as exc:
        with pytest.raises(ApplicationError) as info:
            raise_activity_error(exc)
    assert info.value.type == error.__name__
    assert info.value.non_retryable is True


# --------------------------------------------------------------------------- 台帳（PaidJobRunner）


def _stub_gate(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """担当 B の gate の代わり: dispatch の UPDATE の前に拒否する（行は変えない）。"""

    async def refuse(self: ProviderReservationRepository, reservation_id: str) -> Any:
        raise error

    monkeypatch.setattr(ProviderReservationRepository, "mark_dispatched", refuse)


async def _spec(session_factory) -> PaidJobSpec:
    async with session_factory() as session:
        episode = await EpisodeRepository(session).create(topic="t")
        await session.commit()
    return PaidJobSpec(
        episode_id=episode.id,
        scene_id="sb1",
        provider=ProviderCall.FAL_IMAGE,
        artifact_type=ArtifactType.SCENE_IMAGE,
        input_hash="h" * 64,
        round=1,
    )


async def _reservations(session_factory, episode_id: str):
    async with session_factory() as session:
        return await ProviderReservationRepository(session).list_for_episode_provider(
            episode_id, ProviderCall.FAL_IMAGE
        )


@pytest.mark.parametrize("error", GATE_ERRORS)
async def test_held_dispatch_leaves_an_unsent_reservation_that_resume_reuses(
    session_factory, artifact_store, tmp_path, monkeypatch, error
) -> None:
    runner = PaidJobRunner(
        session_factory=session_factory,
        store=artifact_store,
        workdir=WorkDirectory(tmp_path / "work", forbidden=()),
    )
    spec = await _spec(session_factory)
    generator = FakeImageGenerator()
    request = ImageRequest(prompt="p", width=1080, height=1920, aspect="9:16")

    with monkeypatch.context() as patch:
        _stub_gate(patch, error("episode is held"))
        with pytest.raises(error):  # 成否不明（ProviderSubmitAmbiguousError）に包まない
            await runner.submit(spec, generator, request)

    assert generator.submit_calls == 0  # provider へは送っていない
    [held] = await _reservations(session_factory, spec.episode_id)
    assert held.status is ReservationStatus.RESERVED and held.dispatched_at is None

    # 再開（gate が通す）: 同じ予約で送る。新しい予約は作らない
    outcome = await runner.submit(spec, generator, request)
    assert isinstance(outcome, Submitted) and outcome.reservation_id == held.id
    [after] = await _reservations(session_factory, spec.episode_id)
    assert after.id == held.id and after.dispatched_at is not None
    assert generator.submit_calls == 1


# --------------------------------------------------------------------------- workflow


def _artifact(label: str) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=str(uuid.uuid5(uuid.NAMESPACE_OID, label)),
        object_key=f"k/{label}",
        sha256="a" * 64,
        reused=False,
    )


@dataclass
class _Recorder:
    """1シーン。image submit が gate に拒否された時の Production workflow を見る。"""

    held_type: str = EpisodeHeldError.__name__
    image_submits: int = 0
    plans: list[PlanSceneAlternativeRequest] = field(default_factory=list)
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
                images=[SceneImageWork(scene_id="sb1")],
                videos=[SceneVideoWork(scene_id="sb1", requested_duration_ms=4000)],
                voices=[SceneVoiceWork(script_scene_id="s1", storyboard_scene_ids=["sb1"])],
            )

        @activity.defn(name=PRODUCTION_ASSEMBLE_MANIFEST)
        async def assemble(req: ProductionAssembleRequest) -> SceneArtifactResult:
            return _artifact("manifest")

        @activity.defn(name=PRODUCTION_MARK_READY)
        async def mark_ready(req: ProductionMarkReadyRequest) -> ProductionMarkReadyResult:
            return ProductionMarkReadyResult(status="assets_ready")

        @activity.defn(name=PRODUCTION_RECORD_FAILURE)
        async def record_failure(req: ProductionRecordFailureRequest) -> ProductionFailureOutcome:
            self.failures.append(req)
            status = "blocked" if req.failure_class == FailureClass.NEEDS_INPUT.value else "failed"
            return ProductionFailureOutcome(episode_status=status)

        return [admit, plan, assemble, mark_ready, record_failure]

    def media(self) -> list[Callable[..., Any]]:
        @activity.defn(name=IMAGE_SUBMIT)
        async def image_submit(req: ImageSubmitRequest) -> SubmitResult:
            self.image_submits += 1
            # 本物の Activity は raise_activity_error で同じ形にする（上の境界テスト）
            raise ApplicationError(
                f"{self.held_type}: episode is held", type=self.held_type, non_retryable=True
            )

        @activity.defn(name=IMAGE_AWAIT)
        async def image_await(req: ImageAwaitRequest) -> SceneArtifactResult:
            raise AssertionError("must not await an image that was never submitted")

        @activity.defn(name=VIDEO_SUBMIT)
        async def video_submit(req: VideoSubmitRequest) -> SubmitResult:
            raise AssertionError("video must not start without its image")

        @activity.defn(name=VIDEO_AWAIT)
        async def video_await(req: VideoAwaitRequest) -> SceneArtifactResult:
            raise AssertionError("unreachable")

        @activity.defn(name=VOICE_GENERATE)
        async def voice(req: VoiceGenerateRequest) -> SceneArtifactResult:
            return _artifact("voice")

        return [image_submit, image_await, video_submit, video_await, voice]

    def planner(self) -> list[Callable[..., Any]]:
        @activity.defn(name=PLAN_SCENE_ALTERNATIVE)
        async def plan(req: PlanSceneAlternativeRequest) -> SceneAlternativeOutcome:
            self.plans.append(req)
            raise AssertionError("a hold is not a provider rejection")

        return [plan]


@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


async def _run(env: WorkflowEnvironment, recorder: _Recorder):
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
                episode_id="ep-held",
                image_task_queue=queues["media"],
                video_task_queue=queues["media"],
                voice_task_queue=queues["media"],
                scene_alternative_task_queue=queues["planner"],
            ),
            id=f"episode-ep-held-production-{suffix}",
            task_queue=queues["production"],
        )
    finally:
        for worker in reversed(workers):
            await worker.__aexit__(None, None, None)


@pytest.mark.parametrize("error", GATE_ERRORS)
async def test_held_dispatch_blocks_the_episode_once_without_retry_or_recovery(env, error) -> None:
    recorder = _Recorder(held_type=error.__name__)

    result = await _run(env, recorder)

    assert result.status == "blocked"
    [failure] = recorder.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value
    assert error.__name__ in failure.error_summary
    assert recorder.image_submits == 1  # Activity の retry も新しいラウンドも無い
    assert recorder.plans == []  # 代替映像案（拒否の復旧）に入らない
