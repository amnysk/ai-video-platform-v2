"""Daily Schedule を実 Temporal（localhost:7233）に登録して trigger する（ADR-0023）。

- 本番の Schedule id（``avp-daily-episode``）と本番の task queue には触れない。
  一意な id / queue を作る
- Schedule は paused・遠い cron で作り、``trigger()`` でだけ起動する。最後に必ず削除する
- Activity は stub（DB・有料 provider に到達しない）
- Topic Planner も同じ名前の stub（同じ一意 queue）。子 id が本番と衝突しないよう
  strategy profile id にも一意な接尾辞を付ける
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from temporalio import activity, workflow
from temporalio.client import Client, ScheduleActionExecutionStartWorkflow
from temporalio.worker import Worker

from contracts.pipeline import (
    DAILY_SCHEDULE_ID,
    PIPELINE_CHECK_PAUSED,
    PIPELINE_CLAIM_DAILY_SLOT,
    PIPELINE_TASK_QUEUE,
    PIPELINE_UPLOAD_GATE,
    CheckPausedRequest,
    CheckPausedResult,
    ClaimDailySlotRequest,
    ClaimDailySlotResult,
    ClaimOutcome,
    DailyEpisodeInput,
    PipelineOptions,
    UploadGateRequest,
    UploadGateResult,
)
from contracts.topic_planning import TopicPlannerInput, TopicPlannerResult
from infrastructure.temporal.schedules import ensure_daily_episode_schedule
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

TEMPORAL_ADDRESS = "localhost:7233"


@workflow.defn(name="TopicPlannerWorkflow")
class StubTopicPlanner:
    @workflow.run
    async def run(self, req: TopicPlannerInput) -> TopicPlannerResult:
        return TopicPlannerResult(
            topic_plan_id="00000000-0000-0000-0000-000000000001",
            topic=f"stub topic {req.plan_date}",
            reused=False,
            analytics_mode="no_analytics",
        )


#: 2月30日は来ない: 自動では決して発火しない cron
NEVER_CRON = "0 0 30 2 *"


async def _connect() -> Client:
    try:
        return await asyncio.wait_for(Client.connect(TEMPORAL_ADDRESS), timeout=5)
    except Exception as exc:  # noqa: BLE001 - 到達不能なら skip
        pytest.skip(f"Temporal not reachable at {TEMPORAL_ADDRESS}: {exc}")


@pytest.mark.asyncio
async def test_schedule_trigger_starts_one_daily_run_and_ensure_is_idempotent() -> None:
    client = await _connect()
    suffix = uuid.uuid4().hex[:12]
    schedule_id = f"avp-test-daily-{suffix}"
    queue = f"pipeline-it-{suffix}"
    prefix = f"daily-episode-it-{suffix}"
    assert schedule_id != DAILY_SCHEDULE_ID and queue != PIPELINE_TASK_QUEUE

    claims: list[ClaimDailySlotRequest] = []
    claimed = asyncio.Event()

    @activity.defn(name=PIPELINE_CHECK_PAUSED)
    async def check_paused(req: CheckPausedRequest) -> CheckPausedResult:
        return CheckPausedResult(paused=False)

    @activity.defn(name=PIPELINE_CLAIM_DAILY_SLOT)
    async def claim(req: ClaimDailySlotRequest) -> ClaimDailySlotResult:
        claims.append(req)
        claimed.set()
        # 子 pipeline を起動させない（この test は Schedule → Daily の配線だけを見る）
        return ClaimDailySlotResult(outcome=ClaimOutcome.LIMIT_REACHED)

    @activity.defn(name=PIPELINE_UPLOAD_GATE)
    async def gate(req: UploadGateRequest) -> UploadGateResult:
        return UploadGateResult(allowed=False, reason="stub")

    wf_input = DailyEpisodeInput(
        daily_limit=1,
        strategy_profile_id=f"it_{suffix}",
        options=PipelineOptions(
            pipeline_task_queue=queue, topic_planner_workflow=("TopicPlannerWorkflow", queue)
        ),
    )
    handle = client.get_schedule_handle(schedule_id)
    try:
        outcome = await ensure_daily_episode_schedule(
            client,
            schedule_id=schedule_id,
            cron=NEVER_CRON,
            timezone="Asia/Tokyo",
            daily_limit=1,
            workflow_input=wf_input,
            paused=True,
            task_queue=queue,
            workflow_id_prefix=prefix,
        )
        assert outcome == "created"

        # 再 ensure は作り直さず更新する（上限の変更が反映される）
        again = await ensure_daily_episode_schedule(
            client,
            schedule_id=schedule_id,
            cron=NEVER_CRON,
            timezone="Asia/Tokyo",
            daily_limit=2,
            workflow_input=wf_input,
            paused=True,
            task_queue=queue,
            workflow_id_prefix=prefix,
        )
        assert again == "updated"
        desc = await handle.describe()
        assert desc.schedule.state.paused is True
        assert desc.schedule.spec.time_zone_name == "Asia/Tokyo"
        matching = [s async for s in await client.list_schedules() if s.id == schedule_id]
        assert len(matching) <= 1

        async with Worker(
            client,
            task_queue=queue,
            workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow, StubTopicPlanner],
            activities=[check_paused, claim, gate],
        ):
            await handle.trigger()
            await asyncio.wait_for(claimed.wait(), timeout=60)
            # 走り終わるのを待つ（trigger 1 回 = run 1 本）
            for _ in range(100):
                desc = await handle.describe()
                if desc.info.num_actions >= 1 and not desc.info.running_actions:
                    break
                await asyncio.sleep(0.2)

        assert desc.info.num_actions == 1
        assert len(claims) == 1
        assert claims[0].trigger_id.startswith(prefix)
        assert claims[0].daily_limit == 2
        assert len(claims[0].slot_date) == 10
        assert claims[0].topic_plan_id == "00000000-0000-0000-0000-000000000001"
        recent = desc.info.recent_actions
        assert len(recent) == 1
        action = recent[0].action
        assert isinstance(action, ScheduleActionExecutionStartWorkflow)
        run = client.get_workflow_handle(action.workflow_id, run_id=action.first_execution_run_id)
        result = await run.result()
        assert result["outcome"] == "limit_reached"
    finally:
        with contextlib.suppress(Exception):
            await handle.delete()
