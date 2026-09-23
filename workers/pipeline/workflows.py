"""DailyEpisodeWorkflow / EpisodePipelineWorkflow（ADR-0023）。

**工程の順序を知る唯一の場所**（INV-4 / INV-5）。I/O をしない::

    Schedule（avp-daily-episode）→ DailyEpisodeWorkflow
        check_paused
        → 子 TopicPlannerWorkflow（id topic-plan-{日}-{strategy}-{content}）の完了を待つ（ADR-0025）
          失敗したら Daily も失敗し、Episode を作らない（INV-21）
        → claim_daily_slot（trigger id = workflow id で冪等。plan を Episode に結び付ける）
        → Episode に plan があるときだけ子 EpisodePipelineWorkflow（id episode-{id}-pipeline、
          ABANDON）を起動して終わる
    EpisodePipelineWorkflow
        Script → Storyboard → Production → Render → upload_gate → Upload
        子が駐機点以外を返す・失敗する・同じ id がすでに走っている → そこで止まって結果を返す
        ``EpisodePipelineInput.start_stage`` より前の工程はスキップする（統一再開、ADR-0032）。
        既定は ``SCRIPT``（最初から）

子 workflow は**名前と task queue**で起動する（``contracts.pipeline``）。
実装を import しない（INV-3）。
Episode は互いに独立。ある日の Episode が止まっても翌日の trigger は新しい slot を取る（INV-13）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, SearchAttributeKey, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, ChildWorkflowError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from contracts.pipeline import (
        DAILY_EPISODE_WORKFLOW,
        EPISODE_PIPELINE_WORKFLOW,
        PIPELINE_CHECK_PAUSED,
        PIPELINE_CLAIM_DAILY_SLOT,
        PIPELINE_UPLOAD_GATE,
        STAGE_PARKING_STATUS,
        TOPIC_PLANNER_BUSY_WAIT_SECONDS,
        TOPIC_PLANNER_START_ATTEMPTS,
        CheckPausedRequest,
        CheckPausedResult,
        ClaimDailySlotRequest,
        ClaimDailySlotResult,
        ClaimOutcome,
        DailyEpisodeInput,
        DailyEpisodeResult,
        DailyOutcome,
        EpisodePipelineInput,
        EpisodePipelineResult,
        PipelineOptions,
        PipelineOutcome,
        PipelineStage,
        UploadGateRequest,
        UploadGateResult,
        local_slot_date,
        pipeline_workflow_id,
        production_workflow_id,
        render_workflow_id,
        script_workflow_id,
        storyboard_workflow_id,
        upload_workflow_id,
    )
    from contracts.topic_planning import (
        TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS,
        TopicPlannerInput,
        TopicPlannerResult,
        topic_plan_workflow_id,
    )

#: ADR-0025 の Planner 導入を履歴に記録する patch id（旧履歴の replay を旧経路へ振り分ける）
TOPIC_PLANNER_PATCH_ID = "topic-planner-0025"

STATE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
STATE_SCHEDULE_TO_CLOSE = timedelta(hours=1)
STATE_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(milliseconds=500),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=0,
)

#: Schedule が起動した workflow に Temporal が付ける検索属性（予定時刻）
SCHEDULED_START_TIME = SearchAttributeKey.for_datetime("TemporalScheduledStartTime")


async def _state_activity(name: str, arg: Any, result_type: type) -> Any:
    return await workflow.execute_activity(
        name,
        arg,
        result_type=result_type,
        start_to_close_timeout=STATE_ACTIVITY_TIMEOUT,
        schedule_to_close_timeout=STATE_SCHEDULE_TO_CLOSE,
        retry_policy=STATE_RETRY_POLICY,
    )


def _trigger_instant() -> datetime:
    info = workflow.info()
    scheduled = info.typed_search_attributes.get(SCHEDULED_START_TIME)
    return scheduled if scheduled is not None else info.workflow_start_time


@workflow.defn(name=DAILY_EPISODE_WORKFLOW[0])
class DailyEpisodeWorkflow:
    @workflow.run
    async def run(self, request: DailyEpisodeInput) -> DailyEpisodeResult:
        slot = self._slot_date(request)
        paused: CheckPausedResult = await _state_activity(
            PIPELINE_CHECK_PAUSED, CheckPausedRequest(), CheckPausedResult
        )
        if paused.paused:
            return DailyEpisodeResult(
                outcome=DailyOutcome.PAUSED, slot_date=slot, reason=paused.reason
            )

        # ADR-0025 より前に始まった実行（marker の無い履歴）は旧経路で replay する:
        # Planner を呼ばず request.topic で claim し、plan の有無を見ずに pipeline を起動する。
        # 旧経路の実行が残っていないことを確かめたら（docs/operations/pipeline-worker.md）
        # deprecate_patch を経て削除できる
        planned = workflow.patched(TOPIC_PLANNER_PATCH_ID)
        # Planner の失敗はここで伝播し、Daily は失敗する（Episode を作らない / INV-21）
        plan = await self._plan_topic(request, slot) if planned else None

        claim: ClaimDailySlotResult = await _state_activity(
            PIPELINE_CLAIM_DAILY_SLOT,
            ClaimDailySlotRequest(
                slot_date=slot,
                trigger_id=workflow.info().workflow_id,
                daily_limit=request.daily_limit,
                topic=plan.topic if plan is not None else request.topic,
                topic_plan_id=plan.topic_plan_id if plan is not None else None,
            ),
            ClaimDailySlotResult,
        )
        if claim.outcome == ClaimOutcome.LIMIT_REACHED or claim.episode_id is None:
            return DailyEpisodeResult(
                outcome=DailyOutcome.LIMIT_REACHED,
                slot_date=slot,
                topic_plan_id=plan.topic_plan_id if plan is not None else None,
                reason=f"daily limit {request.daily_limit} reached for {slot}",
            )
        if planned and claim.topic_plan_id is None:
            # plan の無い Episode の pipeline は始めない（INV-21）
            return DailyEpisodeResult(
                outcome=DailyOutcome.NO_TOPIC_PLAN,
                slot_date=slot,
                episode_id=claim.episode_id,
                reason=f"episode {claim.episode_id} has no topic plan (claim {claim.outcome})",
            )

        child_id = pipeline_workflow_id(claim.episode_id)
        try:
            # 完了を待たない。Daily は「今日の1本を起動した」ことだけに責任を持つ。
            # ALLOW_DUPLICATE_FAILED_ONLY: 完了済みの pipeline を同じ trigger の再実行で
            # 再起動しない。クラッシュで failed になった pipeline だけ再起動できる。
            await workflow.start_child_workflow(
                EPISODE_PIPELINE_WORKFLOW[0],
                EpisodePipelineInput(episode_id=claim.episode_id, options=request.options),
                id=child_id,
                task_queue=request.options.pipeline_task_queue,
                parent_close_policy=ParentClosePolicy.ABANDON,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
            )
        except WorkflowAlreadyStartedError:
            return DailyEpisodeResult(
                outcome=DailyOutcome.ALREADY_STARTED,
                slot_date=slot,
                episode_id=claim.episode_id,
                pipeline_workflow_id=child_id,
                topic_plan_id=claim.topic_plan_id,
                reason=f"{child_id} already started (claim {claim.outcome})",
            )
        return DailyEpisodeResult(
            outcome=DailyOutcome.STARTED,
            slot_date=slot,
            episode_id=claim.episode_id,
            pipeline_workflow_id=child_id,
            topic_plan_id=claim.topic_plan_id,
            reason=f"claim {claim.outcome}",
        )

    @staticmethod
    async def _plan_topic(request: DailyEpisodeInput, slot: str) -> TopicPlannerResult:
        """子 TopicPlannerWorkflow の完了を待つ。

        id は (日, strategy, content) で決まる。ALLOW_DUPLICATE: 完了済みの Planner を再実行すると
        DB の plan を見つけてすぐ返す（再生成しない / INV-22）。同じ id が走っている間は待つ。
        """
        name, queue = request.options.topic_planner_workflow
        child_id = topic_plan_workflow_id(
            slot, request.strategy_profile_id, request.content_profile_id
        )
        planner_input = TopicPlannerInput(
            plan_date=slot,
            strategy_profile_id=request.strategy_profile_id,
            content_profile_id=request.content_profile_id,
        )
        for _ in range(TOPIC_PLANNER_START_ATTEMPTS):
            try:
                return await workflow.execute_child_workflow(
                    name,
                    planner_input,
                    id=child_id,
                    task_queue=queue,
                    result_type=TopicPlannerResult,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    execution_timeout=timedelta(seconds=TOPIC_PLANNER_EXECUTION_TIMEOUT_SECONDS),
                )
            except WorkflowAlreadyStartedError:
                await workflow.sleep(timedelta(seconds=TOPIC_PLANNER_BUSY_WAIT_SECONDS))
        raise ApplicationError(
            f"{child_id} stayed busy for {TOPIC_PLANNER_START_ATTEMPTS} attempts",
            non_retryable=True,
        )

    @staticmethod
    def _slot_date(request: DailyEpisodeInput) -> str:
        try:
            if request.slot_date:
                return date.fromisoformat(request.slot_date).isoformat()
            return local_slot_date(_trigger_instant(), request.timezone).isoformat()
        except (ValueError, KeyError) as exc:
            # 設定誤りは再試行しても直らない。workflow task を回し続けず失敗させる
            raise ApplicationError(
                f"invalid slot date / timezone: {exc}", non_retryable=True
            ) from None


def _stage_request(
    stage: PipelineStage, episode_id: str, options: PipelineOptions
) -> tuple[tuple[str, str], str, dict[str, Any]]:
    """工程の (workflow 名, queue)・workflow id・入力 dict。

    worker の型を import しない（INV-3）。
    """
    if stage is PipelineStage.SCRIPT:
        return options.script_workflow, script_workflow_id(episode_id), {"episode_id": episode_id}
    if stage is PipelineStage.STORYBOARD:
        return (
            options.storyboard_workflow,
            storyboard_workflow_id(episode_id),
            {"episode_id": episode_id},
        )
    if stage is PipelineStage.PRODUCTION:
        p = options.production
        return (
            options.production_workflow,
            production_workflow_id(episode_id),
            {
                "episode_id": episode_id,
                "image_concurrency": p.image_concurrency,
                "video_concurrency": p.video_concurrency,
                "voice_concurrency": p.voice_concurrency,
                "image_max_rounds": p.image_max_rounds,
                "video_max_rounds": p.video_max_rounds,
                "await_reexecutions": p.await_reexecutions,
            },
        )
    if stage is PipelineStage.RENDER:
        return (
            options.render_workflow,
            render_workflow_id(episode_id),
            {"episode_id": episode_id, "render_profile_id": options.render_profile_id},
        )
    return options.upload_workflow, upload_workflow_id(episode_id), {"episode_id": episode_id}


@workflow.defn(name=EPISODE_PIPELINE_WORKFLOW[0])
class EpisodePipelineWorkflow:
    @workflow.run
    async def run(self, request: EpisodePipelineInput) -> EpisodePipelineResult:
        ep = request.episode_id
        result = EpisodePipelineResult(episode_id=ep, outcome=PipelineOutcome.COMPLETED, status="")
        stages = list(PipelineStage)
        start_index = stages.index(PipelineStage(request.start_stage))
        for index, stage in enumerate(stages):
            if index < start_index:
                # 統一再開（ADR-0032）: 途中入場より前の工程は完了済みとして扱う。
                # 子 workflow を起動しない（再課金しない / INV-17）
                result.completed_stages.append(stage.value)
                continue
            if stage is PipelineStage.UPLOAD:
                gate: UploadGateResult = await _state_activity(
                    PIPELINE_UPLOAD_GATE, UploadGateRequest(episode_id=ep), UploadGateResult
                )
                if not gate.allowed:
                    result.outcome = PipelineOutcome.UPLOAD_SKIPPED
                    result.stopped_stage = stage.value
                    result.reason = gate.reason
                    result.status = gate.status or result.status
                    return result

            (name, queue), child_id, payload = _stage_request(stage, ep, request.options)
            try:
                # 子の閉じ方は ABANDON: pipeline を止めても有料の工程・投稿を途中で殺さない
                child = await workflow.execute_child_workflow(
                    name,
                    payload,
                    id=child_id,
                    task_queue=queue,
                    result_type=dict,
                    parent_close_policy=ParentClosePolicy.ABANDON,
                )
            except WorkflowAlreadyStartedError:
                return self._stop(result, stage, f"{child_id} is already running")
            except ChildWorkflowError as err:
                # 例外文は写さない（session URI 等が混ざりうる / INV-20）。型だけを残す。
                # 詳細は子 workflow 自身の履歴と、各工程が DB に残した失敗記録を見る
                cause = err.cause if err.cause is not None else err
                kind = type(cause).__name__
                app_type = getattr(cause, "type", None)
                if app_type and app_type != kind:
                    kind = f"{kind}({app_type})"
                return self._stop(result, stage, f"{child_id} failed: {kind}")

            status = str(child.get("status", "")) if isinstance(child, dict) else ""
            result.status = status
            expected = STAGE_PARKING_STATUS[stage]
            if status != expected:
                return self._stop(result, stage, f"{name} returned {status!r}, expected {expected}")
            result.completed_stages.append(stage.value)
        return result

    @staticmethod
    def _stop(
        result: EpisodePipelineResult, stage: PipelineStage, reason: str
    ) -> EpisodePipelineResult:
        result.outcome = PipelineOutcome.STOPPED
        result.stopped_stage = stage.value
        result.reason = reason
        return result
