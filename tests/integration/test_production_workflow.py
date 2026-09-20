"""ProductionWorkflow の編成（ADR-0017）。

**本物の Temporal サーバ** + 名前で登録した mock Activity。

time-skipping テストサーバは、同じ workflow task で完了した Activity への cancel 要求を
``ACTIVITY_UNKNOWN`` で拒み、workflow task が永久に再試行される（兄弟 cancel の競合で再現）。
cancel の伝播を検査するため compose の Temporal（``TEMPORAL_ADDRESS``）を使い、
task queue はテストごとに一意にして本物の worker と取り合わない。

メディア worker の実装は import しない（INV-3）。状態系 Activity も mock にし、
工程の順序・ラウンド・並行枠・cancel の伝播だけを検査する。DB 付きの検査は integration。
"""

from __future__ import annotations

import asyncio
import itertools
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError, WorkflowHandle
from temporalio.exceptions import ApplicationError, CancelledError
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
from contracts.states import EpisodeStatus, FailureClass
from domain.errors import (
    MediaValidationError,
    ProductionInputMissingError,
    ProviderJobFailedError,
    ProviderPollDeadlineError,
    ProviderRejectedError,
)
from workers.production.run_inspector import TemporalWorkflowRunInspector
from workers.production.workflows import (
    ProductionWorkflow,
    ProductionWorkflowInput,
)

TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS")

pytestmark = pytest.mark.skipif(
    not TEMPORAL_ADDRESS, reason="TEMPORAL_ADDRESS must point at a Temporal server (compose core)"
)
RUN_TIMEOUT_SECONDS = 90
_ids = itertools.count()

SCENES = ["sb1", "sb2", "sb3", "sb4"]
VOICES = ["s1", "s2", "s3"]


def _artifact(label: str, *, reused: bool = False) -> SceneArtifactResult:
    return SceneArtifactResult(
        artifact_id=str(uuid.uuid5(uuid.NAMESPACE_OID, label)),
        object_key=f"k/{label}",
        sha256="a" * 64,
        reused=reused,
    )


def _error(
    exc_type: type[BaseException], message: str = "boom", *, non_retryable: bool = False
) -> ApplicationError:
    return ApplicationError(message, type=exc_type.__name__, non_retryable=non_retryable)


@dataclass
class Mocks:
    """呼び出しを記録する mock Activity 群。挙動は関数を差し替えて変える。"""

    admitted: bool = True
    calls: list[str] = field(default_factory=list)
    image_submits: list[tuple[str, int]] = field(default_factory=list)
    image_awaits: list[tuple[str, str]] = field(default_factory=list)
    video_submits: list[tuple[str, int, str]] = field(default_factory=list)
    video_awaits: list[str] = field(default_factory=list)
    voices: list[str] = field(default_factory=list)
    failures: list[ProductionRecordFailureRequest] = field(default_factory=list)
    cancelled_awaits: list[str] = field(default_factory=list)
    #: (scene, round) -> 例外 / "reuse"
    image_submit_behavior: dict[tuple[str, int], Any] = field(default_factory=dict)
    #: scene -> 失敗させる await 試行回数（ApplicationError のリスト）
    image_await_errors: dict[str, list[ApplicationError]] = field(default_factory=dict)
    voice_errors: dict[str, list[ApplicationError]] = field(default_factory=dict)
    #: scene -> await を cancel まで終わらせない
    hang_image_await: set[str] = field(default_factory=set)
    delay_seconds: float = 0.0
    inflight: dict[str, int] = field(default_factory=lambda: {"image": 0, "video": 0, "voice": 0})
    max_inflight: dict[str, int] = field(
        default_factory=lambda: {"image": 0, "video": 0, "voice": 0}
    )

    def _enter(self, kind: str) -> None:
        self.inflight[kind] += 1
        self.max_inflight[kind] = max(self.max_inflight[kind], self.inflight[kind])

    def _leave(self, kind: str) -> None:
        self.inflight[kind] -= 1

    # ---------------------------------------------------------------- production queue

    def state_activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=PRODUCTION_ADMIT)
        async def admit(req: ProductionAdmitRequest) -> ProductionAdmitResult:
            self.calls.append("admit")
            if self.admitted:
                return ProductionAdmitResult(admitted=True, status="in_progress")
            return ProductionAdmitResult(admitted=False, status="blocked")

        @activity.defn(name=PRODUCTION_PLAN)
        async def plan(req: ProductionPlanRequest) -> ProductionPlan:
            self.calls.append("plan")
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
            self.calls.append("assemble")
            return _artifact("manifest")

        @activity.defn(name=PRODUCTION_MARK_READY)
        async def mark_ready(req: ProductionMarkReadyRequest) -> ProductionMarkReadyResult:
            self.calls.append("mark_ready")
            return ProductionMarkReadyResult(status=EpisodeStatus.ASSETS_READY.value)

        @activity.defn(name=PRODUCTION_RECORD_FAILURE)
        async def record_failure(req: ProductionRecordFailureRequest) -> ProductionFailureOutcome:
            self.calls.append("record_failure")
            self.failures.append(req)
            status = {
                FailureClass.NEEDS_INPUT.value: "blocked",
                FailureClass.PERMANENT.value: "failed",
            }.get(req.failure_class, "blocked")
            return ProductionFailureOutcome(episode_status=status)

        return [admit, plan, assemble, mark_ready, record_failure]

    # ---------------------------------------------------------------- media queues

    def image_activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=IMAGE_SUBMIT)
        async def submit(req: ImageSubmitRequest) -> SubmitResult:
            self.image_submits.append((req.scene_id, req.round))
            behavior = self.image_submit_behavior.get((req.scene_id, req.round))
            if behavior == "reuse":
                return SubmitResult(
                    reservation_id="", artifact=_artifact(f"img-{req.scene_id}", reused=True)
                )
            if isinstance(behavior, tuple):  # (遅延秒, 例外): 兄弟の await が走り出してから失敗する
                await asyncio.sleep(behavior[0])
                raise behavior[1]
            if isinstance(behavior, BaseException):
                raise behavior
            self._enter("image")
            await asyncio.sleep(self.delay_seconds)
            return SubmitResult(reservation_id=f"r-{req.scene_id}-{req.round}")

        @activity.defn(name=IMAGE_AWAIT)
        async def wait(req: ImageAwaitRequest) -> SceneArtifactResult:
            self.image_awaits.append((req.scene_id, req.reservation_id))
            try:
                if req.scene_id in self.hang_image_await:
                    while True:
                        activity.heartbeat()
                        await asyncio.sleep(0.05)
                errors = self.image_await_errors.get(req.scene_id)
                if errors:
                    raise errors.pop(0)
                await asyncio.sleep(self.delay_seconds)
                return _artifact(f"img-{req.scene_id}")
            except asyncio.CancelledError:
                self.cancelled_awaits.append(req.scene_id)
                raise
            finally:
                self._leave("image")

        return [submit, wait]

    def video_activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=VIDEO_SUBMIT)
        async def submit(req: VideoSubmitRequest) -> SubmitResult:
            self.video_submits.append((req.scene_id, req.round, req.source_image_artifact_id))
            self._enter("video")
            await asyncio.sleep(self.delay_seconds)
            return SubmitResult(reservation_id=f"v-{req.scene_id}-{req.round}")

        @activity.defn(name=VIDEO_AWAIT)
        async def wait(req: VideoAwaitRequest) -> SceneArtifactResult:
            self.video_awaits.append(req.scene_id)
            try:
                await asyncio.sleep(self.delay_seconds)
                return _artifact(f"vid-{req.scene_id}")
            finally:
                self._leave("video")

        return [submit, wait]

    def voice_activities(self) -> list[Callable[..., Any]]:
        @activity.defn(name=VOICE_GENERATE)
        async def generate(req: VoiceGenerateRequest) -> SceneArtifactResult:
            self.voices.append(req.script_scene_id)
            errors = self.voice_errors.get(req.script_scene_id)
            if errors:
                raise errors.pop(0)
            self._enter("voice")
            try:
                await asyncio.sleep(self.delay_seconds)
            finally:
                self._leave("voice")
            return _artifact(f"voice-{req.script_scene_id}")

        return [generate]


@pytest_asyncio.fixture
async def env() -> Client:
    return await Client.connect(TEMPORAL_ADDRESS or "", namespace="default")


async def _run(
    client: Client,
    mocks: Mocks,
    *,
    after_start: Callable[[WorkflowHandle[Any, Any]], Awaitable[None]] | None = None,
    **overrides: Any,
):
    suffix = uuid.uuid4().hex[:10]
    queues = {
        "production": f"production-test-{suffix}",
        "image": f"production-image-test-{suffix}",
        "video": f"production-video-test-{suffix}",
        "voice": f"production-voice-test-{suffix}",
    }
    workers = [
        Worker(
            client,
            task_queue=queues["production"],
            workflows=[ProductionWorkflow],
            activities=mocks.state_activities(),
        ),
        Worker(client, task_queue=queues["image"], activities=mocks.image_activities()),
        Worker(client, task_queue=queues["video"], activities=mocks.video_activities()),
        Worker(client, task_queue=queues["voice"], activities=mocks.voice_activities()),
    ]
    for w in workers:
        await w.__aenter__()
    try:
        handle = await client.start_workflow(
            ProductionWorkflow.run,
            ProductionWorkflowInput(
                episode_id="ep-1",
                image_task_queue=queues["image"],
                video_task_queue=queues["video"],
                voice_task_queue=queues["voice"],
                **overrides,
            ),
            id=f"episode-ep-1-production-{suffix}-{next(_ids)}",
            task_queue=queues["production"],
        )
        if after_start is not None:
            await after_start(handle)
        try:
            return await asyncio.wait_for(handle.result(), timeout=RUN_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            history = await handle.fetch_history()
            events = [e.WhichOneof("attributes") for e in history.events]
            desc = await handle.describe()
            pending = [
                (p.activity_type.name, p.state) for p in desc.raw_description.pending_activities
            ]
            raise AssertionError(
                f"workflow did not finish: events={events} pending={pending}"
            ) from exc
    finally:
        for w in reversed(workers):
            await w.__aexit__(None, None, None)


async def test_happy_path_produces_every_scene_then_assembles_and_marks_ready(env) -> None:
    mocks = Mocks()
    result = await _run(env, mocks)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert mocks.calls == ["admit", "plan", "assemble", "mark_ready"]
    assert sorted(mocks.image_submits) == [(s, 1) for s in SCENES]
    assert sorted(s for s, _ in mocks.image_awaits) == SCENES
    assert sorted(mocks.voices) == VOICES
    assert sorted(result.images) == SCENES and sorted(result.videos) == SCENES
    assert sorted(result.voices) == VOICES
    # 動画は同じシーンの画像 Artifact から作る
    for scene, round_number, source in mocks.video_submits:
        assert round_number == 1
        assert source == result.images[scene].artifact_id
    assert result.manifest is not None


async def test_reused_image_skips_await(env) -> None:
    mocks = Mocks(image_submit_behavior={(s, 1): "reuse" for s in SCENES})
    result = await _run(env, mocks)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert mocks.image_awaits == []
    assert all(a.reused for a in result.images.values())
    assert len(mocks.video_submits) == len(SCENES)


async def test_retryable_image_failure_starts_a_second_round(env) -> None:
    mocks = Mocks(
        # メディア worker は round を消費した ProviderJobFailedError を non_retryable で送る
        image_await_errors={"sb2": [_error(ProviderJobFailedError, non_retryable=True)]}
    )
    result = await _run(env, mocks)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert sorted(r for s, r in mocks.image_submits if s == "sb2") == [1, 2]
    # ProviderJobFailedError は await の Temporal retry に回さない（同じ参照は結果が変わらない）
    assert [res for s, res in mocks.image_awaits if s == "sb2"] == ["r-sb2-1", "r-sb2-2"]
    assert mocks.failures == []


async def test_retryable_failure_exhausts_image_rounds_then_records_failure(env) -> None:
    mocks = Mocks(image_await_errors={"sb1": [_error(MediaValidationError)] * 3})
    result = await _run(env, mocks)

    assert sorted(r for s, r in mocks.image_submits if s == "sb1") == [1, 2, 3]
    assert "assemble" not in mocks.calls
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.RETRYABLE.value
    assert failure.retry_exhausted is True
    assert result.failure_class == FailureClass.RETRYABLE.value


async def test_needs_input_stops_without_further_rounds(env) -> None:
    mocks = Mocks(image_submit_behavior={("sb3", 1): _error(ProviderRejectedError, "policy")})
    result = await _run(env, mocks)

    assert [r for s, r in mocks.image_submits if s == "sb3"] == [1]
    assert "assemble" not in mocks.calls and "mark_ready" not in mocks.calls
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value
    assert failure.retry_exhausted is False
    assert result.status == "blocked"


async def test_await_retry_does_not_submit_again(env) -> None:
    mocks = Mocks(image_await_errors={"sb1": [_error(ProviderPollDeadlineError)] * 2})
    result = await _run(env, mocks)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert [r for s, r in mocks.image_submits if s == "sb1"] == [1]
    assert [res for s, res in mocks.image_awaits if s == "sb1"] == ["r-sb1-1"] * 3


async def test_unknown_await_state_reawaits_the_same_reservation_until_the_budget(env) -> None:
    """Temporal retry を使い切った期限切れ（> AWAIT_MAX_ATTEMPTS）でも submit し直さない。

    non_retryable で送ると Activity の retry が尽きた状態を即座に作れる。
    """
    mocks = Mocks(
        image_await_errors={
            "sb1": [_error(ProviderPollDeadlineError, non_retryable=True) for _ in range(10)]
        }
    )
    result = await _run(env, mocks, await_reexecutions=3)

    assert [r for s, r in mocks.image_submits if s == "sb1"] == [1], "新しい submit をしない"
    assert [res for s, res in mocks.image_awaits if s == "sb1"] == ["r-sb1-1"] * 4
    assert "assemble" not in mocks.calls
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.RETRYABLE.value
    assert failure.retry_exhausted is True
    assert result.failure_class == FailureClass.RETRYABLE.value


async def test_unknown_await_state_recovers_by_reawaiting(env) -> None:
    mocks = Mocks(
        image_await_errors={
            "sb2": [_error(ProviderPollDeadlineError, non_retryable=True) for _ in range(2)]
        }
    )
    result = await _run(env, mocks, await_reexecutions=3)

    assert result.status == EpisodeStatus.ASSETS_READY.value
    assert [r for s, r in mocks.image_submits if s == "sb2"] == [1]
    assert [res for s, res in mocks.image_awaits if s == "sb2"] == ["r-sb2-1"] * 3
    assert mocks.failures == []


async def test_workflow_cancel_records_failure_and_stays_cancelled(env) -> None:
    mocks = Mocks(hang_image_await={"sb1"})
    inspector = TemporalWorkflowRunInspector(env)
    seen: dict[str, Any] = {}

    async def cancel_when_awaiting(handle: WorkflowHandle[Any, Any]) -> None:
        for _ in range(400):
            if any(s == "sb1" for s, _ in mocks.image_awaits):
                break
            await asyncio.sleep(0.05)
        desc = await handle.describe()
        seen["run_id"] = desc.run_id
        seen["running_closed"] = await inspector.is_closed(handle.id, desc.run_id)
        seen["id"] = handle.id
        await handle.cancel()

    with pytest.raises(WorkflowFailureError) as info:
        await _run(env, mocks, after_start=cancel_when_awaiting, image_concurrency=4)

    assert isinstance(info.value.cause, CancelledError)
    assert seen["running_closed"] is False, "走っている run は閉じていない"
    assert await inspector.is_closed(seen["id"], seen["run_id"]) is True
    assert await inspector.is_closed(seen["id"], str(uuid.uuid4())) is True  # NotFound
    assert "sb1" in mocks.cancelled_awaits
    (failure,) = mocks.failures
    assert failure.failure_class == FailureClass.NEEDS_INPUT.value
    assert "cancelled" in failure.error_summary
    assert "assemble" not in mocks.calls


async def test_not_admitted_does_nothing(env) -> None:
    mocks = Mocks(admitted=False)
    result = await _run(env, mocks)

    assert result.admitted is False and result.status == "blocked"
    assert mocks.calls == ["admit"]
    assert mocks.image_submits == [] and mocks.voices == [] and mocks.failures == []


@pytest.mark.parametrize(("image", "video", "voice"), [(2, 1, 1), (1, 1, 2)])
async def test_workflow_side_concurrency_is_bounded(env, image, video, voice) -> None:
    mocks = Mocks(delay_seconds=0.2)
    result = await _run(
        env, mocks, image_concurrency=image, video_concurrency=video, voice_concurrency=voice
    )

    assert result.status == EpisodeStatus.ASSETS_READY.value
    # 上限を守り、かつ上限まで実際に並行した（4シーン・3音声・各 0.2 秒で枠が埋まる）
    assert mocks.max_inflight["image"] == image
    assert mocks.max_inflight["video"] == video
    assert mocks.max_inflight["voice"] == voice


async def test_terminal_failure_cancels_in_flight_awaits(env) -> None:
    mocks = Mocks(
        hang_image_await={"sb1"},
        # 音声は画像・動画より先に済む（ADR-0028）ので、兄弟の cancel は別シーンの画像の失敗で起こす
        image_submit_behavior={
            ("sb2", 1): (1.0, _error(ProductionInputMissingError, "no script scene"))
        },
        delay_seconds=0.3,
    )
    result = await _run(env, mocks, image_concurrency=4)

    assert "sb1" in mocks.cancelled_awaits, "進行中の await は heartbeat で cancel を受け取る"
    assert "assemble" not in mocks.calls

    assert len(mocks.failures) == 1, "cancel された枝は失敗として数えない"
    assert mocks.failures[0].failure_class == FailureClass.NEEDS_INPUT.value
    assert result.status == "blocked"


def test_dominant_failure_class_prefers_needs_input_then_permanent_then_retryable() -> None:
    """最初の失敗で兄弟は cancel されるので、複数の失敗は競合時だけ起きる。順序を直接検査する。"""
    from workers.production.workflows import _StageFailure

    wf = ProductionWorkflow()
    wf._failures = [
        _StageFailure(FailureClass.RETRYABLE, "r", retry_exhausted=True),
        _StageFailure(FailureClass.PERMANENT, "p", retry_exhausted=False),
        _StageFailure(FailureClass.NEEDS_INPUT, "n", retry_exhausted=False),
    ]
    assert wf._dominant().failure_class is FailureClass.NEEDS_INPUT
    wf._failures = wf._failures[:2]
    assert wf._dominant().failure_class is FailureClass.PERMANENT
