"""ProductionWorkflow（ADR-0017）。

**工程の順序・ラウンド数・並行数を知る唯一の場所**（INV-4 / INV-5）。I/O をしない。

構造::

    admit → plan →
      並行 { 音声: 台本シーンごとに VOICE_GENERATE（Temporal retry 最大3回）
             シーン: storyboard シーンごとに
                     画像ラウンド [IMAGE_SUBMIT(1回) → 再利用でなければ IMAGE_AWAIT(retry 5回)
                                   → 状態不明の失敗なら同じ予約で再 await（上限つき）]
                     → 動画ラウンド [VIDEO_SUBMIT(1回) → VIDEO_AWAIT(同上)] }
      どれかが終端的に失敗 → 兄弟を cancel → 支配的な失敗クラスで record_failure
      全部成功 → assemble_manifest → mark_ready
      workflow の cancel → 進行中の枝を止め、cancel されない形で record_failure → cancel を再送出

``round`` は**この実行の中の試行番号**（予算の数え方）で、台帳のラウンドではない。
実行をまたいだ実効ラウンドは台帳が (provider, input_hash, scene) の最新予約から決める。

メディア Activity は**名前**で呼ぶ（``contracts.production_activities``）。
実装を import しない（INV-3）。
有料 submit は Temporal に retry させない（INV-15）。新しいラウンドは retryable な失敗のときだけ。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, TypeVar

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, CancelledError
from temporalio.exceptions import TimeoutError as TemporalTimeoutError
from temporalio.workflow import ActivityCancellationType

with workflow.unsafe.imports_passed_through():
    from contracts.production_activities import (
        AWAIT_HEARTBEAT_TIMEOUT_SECONDS,
        AWAIT_MAX_ATTEMPTS,
        AWAIT_START_TO_CLOSE_SECONDS,
        DEFAULT_AWAIT_REEXECUTIONS,
        DEFAULT_IMAGE_CONCURRENCY,
        DEFAULT_IMAGE_MAX_ROUNDS,
        DEFAULT_VIDEO_CONCURRENCY,
        DEFAULT_VIDEO_MAX_ROUNDS,
        DEFAULT_VOICE_CONCURRENCY,
        IMAGE_AWAIT,
        IMAGE_SUBMIT,
        PRODUCTION_ADMIT,
        PRODUCTION_ASSEMBLE_MANIFEST,
        PRODUCTION_MARK_READY,
        PRODUCTION_PLAN,
        PRODUCTION_RECORD_FAILURE,
        SUBMIT_MAX_ATTEMPTS,
        VIDEO_AWAIT,
        VIDEO_SUBMIT,
        VOICE_GENERATE,
        VOICE_MAX_ATTEMPTS,
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
        SceneVideoWork,
        SceneVoiceWork,
        SubmitResult,
        VideoAwaitRequest,
        VideoSubmitRequest,
        VoiceGenerateRequest,
    )
    from contracts.states import (
        PRODUCTION_IMAGE_TASK_QUEUE,
        PRODUCTION_VIDEO_TASK_QUEUE,
        PRODUCTION_VOICE_TASK_QUEUE,
        PRODUCTION_WORKFLOW,
        RETRYABLE_FAILURE_CLASSES,
        FailureClass,
    )
    from domain.errors import (
        FAILURE_CLASS_BY_TYPE_NAME,
        NON_RETRYABLE_ERROR_TYPE_NAMES,
        MediaValidationError,
        ProviderInvocationError,
        ProviderJobFailedError,
        ProviderPollDeadlineError,
        ProviderTimeoutError,
        failure_class_from_type_name,
    )

WORKFLOW_NAME, TASK_QUEUE = PRODUCTION_WORKFLOW

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
#: マニフェストはシーン Artifact を全件読むので状態系より長く取る。
ASSEMBLE_ACTIVITY_TIMEOUT = timedelta(minutes=5)
#: submit はアップロード + provider への投入。adapter の submit timeout（既定120秒）より長い。
SUBMIT_ACTIVITY_TIMEOUT = timedelta(minutes=5)
#: ローカル TTS 1シーン分。
VOICE_ACTIVITY_TIMEOUT = timedelta(minutes=10)

#: 状態系 Activity（admit / plan / assemble / mark_ready / record_failure）は Episode を
#: ``in_progress`` から出す唯一の経路なので、DB の一時障害で諦めない: 回数無制限・backoff 上限1分・
#: schedule_to_close 1時間。retry しないのは人間の判断が要る / 決定論的な失敗の型だけ。
STATE_SCHEDULE_TO_CLOSE = timedelta(hours=1)
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=200),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

#: 課金 submit は自動 retry しない（INV-15）。retry は workflow のラウンド。
SUBMIT_RETRY_POLICY = RetryPolicy(
    maximum_attempts=SUBMIT_MAX_ATTEMPTS,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

#: await は provider job 参照に対して冪等なので retry してよい。ただし provider 側のジョブ失敗と
#: メディア検査の失敗は、同じ参照を待ち直しても結果が変わらない → 次のラウンドへ回す。
AWAIT_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=AWAIT_MAX_ATTEMPTS,
    non_retryable_error_types=[
        *NON_RETRYABLE_ERROR_TYPE_NAMES,
        ProviderJobFailedError.__name__,
        MediaValidationError.__name__,
    ],
)

#: await が失敗しても provider 側ジョブの状態が**分からない**型。同じ予約で待ち直す
#: （新しいラウンド = 新しい submit にすると、まだ走っているジョブと二重課金になりうる）。
#: 加えて Temporal の timeout（start_to_close / heartbeat）と transient 全般。
REAWAIT_ERROR_TYPE_NAMES: frozenset[str] = frozenset(
    {
        ProviderPollDeadlineError.__name__,
        ProviderTimeoutError.__name__,
        ProviderInvocationError.__name__,
        *(n for n, c in FAILURE_CLASS_BY_TYPE_NAME.items() if c is FailureClass.TRANSIENT),
    }
)
#: ジョブが**確定的に終わった**型。新しいラウンドへ進んでよいのはこれだけ。
NEW_ROUND_ERROR_TYPE_NAMES: frozenset[str] = frozenset(
    {ProviderJobFailedError.__name__, MediaValidationError.__name__}
)

#: ローカル非課金の音声合成（ADR-0017 §5 の限定例外）。
VOICE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=VOICE_MAX_ATTEMPTS,
    non_retryable_error_types=list(NON_RETRYABLE_ERROR_TYPE_NAMES),
)

#: 複数の枝が失敗したときに Episode へ記録する失敗クラスの優先順（小さいほど支配的）。
#: needs_input は人間の判断が要り（blocked）、permanent より先に人間に見せる（Episode を
#: terminal failed にしてよいのは他に回復の余地が無いときだけ / failure-policy §2）。
FAILURE_PRECEDENCE: dict[FailureClass, int] = {
    FailureClass.NEEDS_INPUT: 0,
    FailureClass.PERMANENT: 1,
    FailureClass.RETRYABLE: 2,
    FailureClass.TRANSIENT: 3,
}

T = TypeVar("T")


@dataclass
class ProductionWorkflowInput:
    episode_id: str
    #: workflow 側で同時に走らせる submit+await の上限（worker の並行数設定と揃える）。
    #: provider へ未消化の submit を積み上げないための枠。
    image_concurrency: int = DEFAULT_IMAGE_CONCURRENCY
    video_concurrency: int = DEFAULT_VIDEO_CONCURRENCY
    voice_concurrency: int = DEFAULT_VOICE_CONCURRENCY
    #: この実行の中での submit 試行の予算（台帳のラウンド番号ではない）
    image_max_rounds: int = DEFAULT_IMAGE_MAX_ROUNDS
    video_max_rounds: int = DEFAULT_VIDEO_MAX_ROUNDS
    #: 状態不明の await 失敗に対し、同じ予約で await を追加実行する回数（既定は契約の定数）
    await_reexecutions: int = DEFAULT_AWAIT_REEXECUTIONS
    #: メディア Activity の task queue。既定は契約の定数。テストが共有サーバ上で
    #: 本物のメディア worker と取り合わないように差し替えられる。
    image_task_queue: str = PRODUCTION_IMAGE_TASK_QUEUE
    video_task_queue: str = PRODUCTION_VIDEO_TASK_QUEUE
    voice_task_queue: str = PRODUCTION_VOICE_TASK_QUEUE


@dataclass
class ProductionWorkflowResult:
    episode_id: str
    #: **application domain state**（INV-8）
    status: str
    admitted: bool = True
    owned: bool = True
    manifest: SceneArtifactResult | None = None
    failure_class: str | None = None
    images: dict[str, SceneArtifactResult] = field(default_factory=dict)
    videos: dict[str, SceneArtifactResult] = field(default_factory=dict)
    voices: dict[str, SceneArtifactResult] = field(default_factory=dict)


@dataclass
class _StageFailure(Exception):
    failure_class: FailureClass
    summary: str
    retry_exhausted: bool


def _failure_class(err: ActivityError) -> FailureClass:
    """失敗クラスは**例外の型名**から引く。未知・timeout は ``needs_input``（INV-12）。"""
    cause = err.cause
    type_name = cause.type if isinstance(cause, ApplicationError) else None
    return failure_class_from_type_name(type_name)


def _await_failure_is_unknown_state(err: ActivityError) -> bool:
    cause = err.cause
    if isinstance(cause, TemporalTimeoutError):
        return True
    return isinstance(cause, ApplicationError) and cause.type in REAWAIT_ERROR_TYPE_NAMES


def _await_failure_ends_job(err: ActivityError) -> bool:
    cause = err.cause
    return isinstance(cause, ApplicationError) and cause.type in NEW_ROUND_ERROR_TYPE_NAMES


def _reraise_if_cancelled(err: ActivityError) -> None:
    """兄弟の失敗で cancel された Activity は失敗として数えない。

    分類すると needs_input に化けて支配的な失敗クラスを歪める。
    """
    if isinstance(err.cause, CancelledError):
        raise asyncio.CancelledError() from err


def _summary(err: BaseException) -> str:
    cause = err.cause if isinstance(err, ActivityError) else None  # type: ignore[union-attr]
    return str(cause if cause is not None else err)[:1000]


@workflow.defn(name=WORKFLOW_NAME)
class ProductionWorkflow:
    def __init__(self) -> None:
        self._failures: list[_StageFailure] = []
        self._tasks: list[asyncio.Task[Any]] = []

    @workflow.run
    async def run(self, request: ProductionWorkflowInput) -> ProductionWorkflowResult:
        info = workflow.info()
        episode_id = request.episode_id
        admit: ProductionAdmitResult = await workflow.execute_activity(
            PRODUCTION_ADMIT,
            ProductionAdmitRequest(
                episode_id=episode_id, workflow_id=info.workflow_id, run_id=info.run_id
            ),
            result_type=ProductionAdmitResult,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        if not admit.admitted:
            return ProductionWorkflowResult(
                episode_id=episode_id, status=admit.status, admitted=False
            )
        try:
            return await self._admitted(request)
        except asyncio.CancelledError:
            # 入場後の cancel。Episode を in_progress に置き去りにしない。
            # 人間が止めたので needs_input → blocked（POST で再開 / ADR-0017 §8）。
            reason = workflow.cancellation_reason()
            await self._settle_uncancellable(
                request,
                _StageFailure(
                    FailureClass.NEEDS_INPUT,
                    f"production workflow cancelled{f': {reason}' if reason else ''}",
                    retry_exhausted=False,
                ),
            )
            raise

    async def _settle_uncancellable(
        self, request: ProductionWorkflowInput, failure: _StageFailure
    ) -> None:
        """cancel を受けた後の記録。重ねて cancel されても記録の完了を待ってから抜ける。

        Python SDK の workflow cancel は主タスクへの ``Task.cancel()`` 1回で、捕まえた後の
        Activity 呼び出しは cancel されない。``asyncio.shield`` は重ねての cancel への保険。
        """
        settle = asyncio.ensure_future(self._settle(request, failure))
        while True:
            try:
                await asyncio.shield(settle)
                return
            except asyncio.CancelledError:
                if settle.done():
                    raise

    async def _admitted(self, request: ProductionWorkflowInput) -> ProductionWorkflowResult:
        info = workflow.info()
        episode_id = request.episode_id
        try:
            plan: ProductionPlan = await workflow.execute_activity(
                PRODUCTION_PLAN,
                ProductionPlanRequest(
                    episode_id=episode_id, workflow_id=info.workflow_id, run_id=info.run_id
                ),
                result_type=ProductionPlan,
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
                retry_policy=STATE_RETRY_POLICY,
            )
        except ActivityError as err:
            _reraise_if_cancelled(err)
            return await self._settle(
                request, _StageFailure(_failure_class(err), _summary(err), retry_exhausted=False)
            )

        result = ProductionWorkflowResult(episode_id=episode_id, status="")
        await self._produce(request, plan, result)
        if self._failures:
            return await self._settle(request, self._dominant())

        try:
            manifest = await workflow.execute_activity(
                PRODUCTION_ASSEMBLE_MANIFEST,
                ProductionAssembleRequest(
                    episode_id=episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    storyboard_artifact_id=plan.storyboard_artifact_id,
                    script_artifact_id=plan.script_artifact_id,
                ),
                result_type=SceneArtifactResult,
                start_to_close_timeout=ASSEMBLE_ACTIVITY_TIMEOUT,
                schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
                retry_policy=STATE_RETRY_POLICY,
            )
        except ActivityError as err:
            _reraise_if_cancelled(err)
            cls = _failure_class(err)
            return await self._settle(
                request,
                _StageFailure(cls, _summary(err), retry_exhausted=cls in RETRYABLE_FAILURE_CLASSES),
            )

        try:
            ready: ProductionMarkReadyResult = await workflow.execute_activity(
                PRODUCTION_MARK_READY,
                ProductionMarkReadyRequest(
                    episode_id=episode_id, workflow_id=info.workflow_id, run_id=info.run_id
                ),
                result_type=ProductionMarkReadyResult,
                start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
                schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
                retry_policy=STATE_RETRY_POLICY,
            )
        except ActivityError as err:
            _reraise_if_cancelled(err)
            raise
        result.status = ready.status
        result.owned = ready.owned
        result.manifest = manifest
        return result

    # ------------------------------------------------------------------ 並行制作

    async def _produce(
        self,
        request: ProductionWorkflowInput,
        plan: ProductionPlan,
        result: ProductionWorkflowResult,
    ) -> None:
        image_slots = asyncio.Semaphore(max(1, request.image_concurrency))
        video_slots = asyncio.Semaphore(max(1, request.video_concurrency))
        voice_slots = asyncio.Semaphore(max(1, request.voice_concurrency))
        videos = {v.scene_id: v for v in plan.videos}

        for voice in plan.voices:
            self._spawn(lambda v=voice: self._voice(request, plan, v, voice_slots, result))
        for image in plan.images:
            video = videos[image.scene_id]
            self._spawn(
                lambda v=video: self._scene(request, plan, v, image_slots, video_slots, result)
            )
        # 例外は各枝の guard が _failures に集める。
        # cancel された枝の CancelledError もここで吸収する。
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _spawn(self, branch: Callable[[], Awaitable[None]]) -> None:
        async def guard() -> None:
            try:
                await branch()
            except _StageFailure as failure:
                self._fail(failure)

        self._tasks.append(asyncio.create_task(guard()))

    def _fail(self, failure: _StageFailure) -> None:
        workflow.logger.warning(
            "production branch failed class=%s: %s", failure.failure_class, failure.summary
        )
        first = not self._failures
        self._failures.append(failure)
        if first:
            # 兄弟を止める。進行中の await は heartbeat で cancel を受け取る。
            # provider 側のジョブは止めない（ADR-0017 §4）。台帳から次の実行で回収できる。
            for task in self._tasks:
                if not task.done():
                    task.cancel()

    def _dominant(self) -> _StageFailure:
        return min(self._failures, key=lambda f: FAILURE_PRECEDENCE[f.failure_class])

    async def _voice(
        self,
        request: ProductionWorkflowInput,
        plan: ProductionPlan,
        work: SceneVoiceWork,
        slots: asyncio.Semaphore,
        result: ProductionWorkflowResult,
    ) -> None:
        info = workflow.info()
        async with slots:
            try:
                artifact = await workflow.execute_activity(
                    VOICE_GENERATE,
                    VoiceGenerateRequest(
                        episode_id=request.episode_id,
                        workflow_id=info.workflow_id,
                        run_id=info.run_id,
                        script_scene_id=work.script_scene_id,
                        storyboard_scene_ids=list(work.storyboard_scene_ids),
                        storyboard_artifact_id=plan.storyboard_artifact_id,
                        script_artifact_id=plan.script_artifact_id,
                    ),
                    result_type=SceneArtifactResult,
                    task_queue=request.voice_task_queue,
                    start_to_close_timeout=VOICE_ACTIVITY_TIMEOUT,
                    retry_policy=VOICE_RETRY_POLICY,
                )
            except ActivityError as err:
                _reraise_if_cancelled(err)
                cls = _failure_class(err)
                raise _StageFailure(
                    cls, _summary(err), retry_exhausted=cls in RETRYABLE_FAILURE_CLASSES
                ) from err
        result.voices[work.script_scene_id] = artifact

    async def _scene(
        self,
        request: ProductionWorkflowInput,
        plan: ProductionPlan,
        work: SceneVideoWork,
        image_slots: asyncio.Semaphore,
        video_slots: asyncio.Semaphore,
        result: ProductionWorkflowResult,
    ) -> None:
        info = workflow.info()
        scene_id = work.scene_id

        def image_submit(round_number: int) -> Awaitable[SubmitResult]:
            return workflow.execute_activity(
                IMAGE_SUBMIT,
                ImageSubmitRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    scene_id=scene_id,
                    storyboard_artifact_id=plan.storyboard_artifact_id,
                    round=round_number,
                ),
                result_type=SubmitResult,
                task_queue=request.image_task_queue,
                start_to_close_timeout=SUBMIT_ACTIVITY_TIMEOUT,
                retry_policy=SUBMIT_RETRY_POLICY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

        def image_await(reservation_id: str) -> Awaitable[SceneArtifactResult]:
            return _execute_await(
                IMAGE_AWAIT,
                ImageAwaitRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    scene_id=scene_id,
                    storyboard_artifact_id=plan.storyboard_artifact_id,
                    reservation_id=reservation_id,
                ),
                request.image_task_queue,
            )

        image = await _rounds(
            request.image_max_rounds,
            request.await_reexecutions,
            image_slots,
            image_submit,
            image_await,
        )
        result.images[scene_id] = image

        def video_submit(round_number: int) -> Awaitable[SubmitResult]:
            return workflow.execute_activity(
                VIDEO_SUBMIT,
                VideoSubmitRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    scene_id=scene_id,
                    storyboard_artifact_id=plan.storyboard_artifact_id,
                    source_image_artifact_id=image.artifact_id,
                    requested_duration_ms=work.requested_duration_ms,
                    round=round_number,
                ),
                result_type=SubmitResult,
                task_queue=request.video_task_queue,
                start_to_close_timeout=SUBMIT_ACTIVITY_TIMEOUT,
                retry_policy=SUBMIT_RETRY_POLICY,
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            )

        def video_await(reservation_id: str) -> Awaitable[SceneArtifactResult]:
            return _execute_await(
                VIDEO_AWAIT,
                VideoAwaitRequest(
                    episode_id=request.episode_id,
                    workflow_id=info.workflow_id,
                    run_id=info.run_id,
                    scene_id=scene_id,
                    storyboard_artifact_id=plan.storyboard_artifact_id,
                    source_image_artifact_id=image.artifact_id,
                    requested_duration_ms=work.requested_duration_ms,
                    reservation_id=reservation_id,
                ),
                request.video_task_queue,
            )

        result.videos[scene_id] = await _rounds(
            request.video_max_rounds,
            request.await_reexecutions,
            video_slots,
            video_submit,
            video_await,
        )

    # ------------------------------------------------------------------ 失敗

    async def _settle(
        self, request: ProductionWorkflowInput, failure: _StageFailure
    ) -> ProductionWorkflowResult:
        info = workflow.info()
        outcome: ProductionFailureOutcome = await workflow.execute_activity(
            PRODUCTION_RECORD_FAILURE,
            ProductionRecordFailureRequest(
                episode_id=request.episode_id,
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                failure_class=failure.failure_class.value,
                error_summary=failure.summary,
                retry_exhausted=failure.retry_exhausted,
            ),
            result_type=ProductionFailureOutcome,
            start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
            schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
            retry_policy=STATE_RETRY_POLICY,
        )
        return ProductionWorkflowResult(
            episode_id=request.episode_id,
            status=outcome.episode_status,
            owned=outcome.owned,
            failure_class=failure.failure_class.value,
        )


def _execute_await(activity_name: str, arg: Any, task_queue: str) -> Awaitable[SceneArtifactResult]:
    return workflow.execute_activity(
        activity_name,
        arg,
        result_type=SceneArtifactResult,
        task_queue=task_queue,
        start_to_close_timeout=timedelta(seconds=AWAIT_START_TO_CLOSE_SECONDS),
        heartbeat_timeout=timedelta(seconds=AWAIT_HEARTBEAT_TIMEOUT_SECONDS),
        retry_policy=AWAIT_RETRY_POLICY,
        # cancel は heartbeat で Activity に届く（poll をやめる）。Activity が cancel を確認して
        # 終わるまで待つ: 待たずに record_failure すると、まだ走っている await が job / 予約を
        # 書く。固まった worker でも heartbeat timeout（90秒）で確定する。
        cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    )


async def _rounds(
    max_rounds: int,
    await_reexecutions: int,
    slots: asyncio.Semaphore,
    submit: Callable[[int], Awaitable[SubmitResult]],
    wait: Callable[[str], Awaitable[SceneArtifactResult]],
) -> SceneArtifactResult:
    """1メディア分の試行ループ。

    - submit は1回（Temporal retry なし）。retryable な submit 失敗は次の試行
    - await は Temporal retry。それでも失敗したら型で分ける:
      - 状態不明（``REAWAIT_ERROR_TYPE_NAMES`` / timeout）: **同じ予約で** await を追加実行
        （``await_reexecutions`` 回まで）。使い切ったら失敗として記録し、新しい submit はしない
      - ジョブが確定的に終わった（``NEW_ROUND_ERROR_TYPE_NAMES``）: 次の試行（新しい submit）
      - それ以外: 失敗として記録

    試行番号は台帳のラウンドではない（台帳が実行をまたいで実効ラウンドを決める）。
    枠（semaphore）は submit から await の完了まで握る: provider に投げて未回収のジョブ数の上限。
    """
    budget = max(1, max_rounds)
    for attempt in range(1, budget + 1):
        async with slots:
            try:
                submitted = await submit(attempt)
            except ActivityError as err:
                _reraise_if_cancelled(err)
                cls = _failure_class(err)
                retryable = cls in RETRYABLE_FAILURE_CLASSES
                if not retryable or attempt >= budget:
                    raise _StageFailure(cls, _summary(err), retry_exhausted=retryable) from err
                workflow.logger.info("production submit %s failed with %s; next", attempt, cls)
                continue
            if submitted.artifact is not None:
                return submitted.artifact  # 同じ入力の現行 Artifact を再利用（INV-17）
            reawaits = 0
            while True:
                try:
                    return await wait(submitted.reservation_id)
                except ActivityError as err:
                    _reraise_if_cancelled(err)
                    cls = _failure_class(err)
                    retryable = cls in RETRYABLE_FAILURE_CLASSES
                    if _await_failure_is_unknown_state(err) and reawaits < await_reexecutions:
                        reawaits += 1
                        workflow.logger.info(
                            "production await %s state unknown; re-await %s/%s",
                            submitted.reservation_id,
                            reawaits,
                            await_reexecutions,
                        )
                        continue
                    if _await_failure_ends_job(err) and attempt < budget:
                        workflow.logger.info(
                            "production attempt %s job over with %s; next attempt", attempt, cls
                        )
                        break
                    raise _StageFailure(cls, _summary(err), retry_exhausted=retryable) from err
    raise AssertionError("unreachable")  # pragma: no cover


__all__ = [
    "AWAIT_RETRY_POLICY",
    "FAILURE_PRECEDENCE",
    "NEW_ROUND_ERROR_TYPE_NAMES",
    "REAWAIT_ERROR_TYPE_NAMES",
    "STATE_RETRY_POLICY",
    "SUBMIT_RETRY_POLICY",
    "TASK_QUEUE",
    "VOICE_RETRY_POLICY",
    "WORKFLOW_NAME",
    "ProductionWorkflow",
    "ProductionWorkflowInput",
    "ProductionWorkflowResult",
]
