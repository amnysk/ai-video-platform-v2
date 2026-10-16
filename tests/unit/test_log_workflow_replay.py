"""Workflow のログ発行は決定性を崩さず、replay で業務イベントを重複発行しない（INV-40）。

本物の ``DailyEpisodeWorkflow`` / ``EpisodePipelineWorkflow``（sandbox あり、本番と同じ）を、
**キャッシュ無し**（``max_cached_workflows=0``: 毎 workflow task で履歴を頭から replay する）の
Worker で走らせる。replay のたびに発行が漏れていれば、同じイベントが複数回出る。
最後に取った履歴を Replayer にかけ、非決定にならないこと・1件も発行しないことを確かめる。
理由は docs/testing/logging-rationale.md。
"""

from __future__ import annotations

import uuid
from collections import Counter

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from contracts.pipeline import DailyEpisodeInput, PipelineOutcome, pipeline_workflow_id
from infrastructure.logging.formatter import workflow_event_id
from tests.support.log_capture import capture_json
from tests.unit.test_pipeline_workflows import (
    FAKE_STAGES,
    Env,
    fake_check_paused,
    fake_claim,
    fake_gate,
)
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

WORKFLOW_LOGGER = "temporalio.workflow"


async def test_workflow_events_are_emitted_once_and_replay_emits_nothing() -> None:
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        env = Env(wf_env.client, f"log-replay-{uuid.uuid4()}", f"log-stages-{uuid.uuid4()}")
        main = Worker(
            wf_env.client,
            task_queue=env.queue,
            workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow],
            activities=[fake_check_paused, fake_claim, fake_gate],
            max_cached_workflows=0,
        )
        stages = Worker(
            wf_env.client,
            task_queue=env.stage_queue,
            workflows=FAKE_STAGES,
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        with capture_json() as logs:
            async with main, stages:
                trigger = f"daily-log-{uuid.uuid4().hex[:8]}"
                daily = await wf_env.client.execute_workflow(
                    "DailyEpisodeWorkflow",
                    DailyEpisodeInput(daily_limit=1, slot_date="2026-09-16", options=env.options()),
                    id=trigger,
                    task_queue=env.queue,
                    result_type=dict,
                )
                episode_id = daily["episode_id"]
                pipeline = await env.pipeline_result(episode_id)
                assert pipeline["outcome"] == PipelineOutcome.COMPLETED
                handle = wf_env.client.get_workflow_handle(pipeline_workflow_id(episode_id))
                history = await handle.fetch_history()

        events = [e for e in logs.events() if e["logger"] == WORKFLOW_LOGGER]
        business = [e for e in events if e["event_name"] != "log.record"]
        counts = Counter((e["workflow_id"], e["event_name"], e.get("stage")) for e in business)
        pipeline_id = pipeline_workflow_id(episode_id)
        # 各工程の判断は1回ずつ（キャッシュ無しで毎 task replay しても重複しない）
        assert counts[(pipeline_id, "stage.started", "pipeline")] == 1
        assert counts[(pipeline_id, "stage.succeeded", "pipeline")] == 1
        assert counts[(trigger, "stage.started", "pipeline")] == 1
        assert all(n == 1 for n in counts.values()), counts
        # event_id は全記録で一意（OpenSearch の create が黙って捨てない）
        ids = [e["event_id"] for e in events]
        assert len(ids) == len(set(ids))
        # Workflow の記録の event_id は uuid5 で導いた値（同じ入力なら同じ ID / log-contract §4）
        started = next(
            e
            for e in business
            if e["workflow_id"] == pipeline_id and e["event_name"] == "stage.started"
        )
        candidates = {
            workflow_event_id(pipeline_id, started["run_id"], hl, seq, "stage.started")
            for hl in range(1, 40)
            for seq in range(4)
        }
        assert started["event_id"] in candidates
        assert started["episode_id"] == episode_id

        # replay: 決定的（非決定なら例外）で、業務イベントを1件も出さない
        with capture_json() as replay_logs:
            await Replayer(
                workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow]
            ).replay_workflow(history)
        replayed = [e for e in replay_logs.events() if e["logger"] == WORKFLOW_LOGGER]
        assert replayed == []
