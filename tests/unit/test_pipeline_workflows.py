"""DailyEpisodeWorkflow / EpisodePipelineWorkflow の編成（ADR-0023）。

time-skipping テストサーバ。工程の子 workflow は**同じ名前**で登録した fake
（別 queue・非 sandbox）、
Activity は名前で登録した fake。claim の fake は ``DailyEpisodeSlotRepository.claim`` の意味論
（同じ trigger は同じ slot、上限到達時は未着手の planned を RESUME）をメモリで写したもの。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from contracts.pipeline import (
    PIPELINE_CHECK_PAUSED,
    PIPELINE_CLAIM_DAILY_SLOT,
    PIPELINE_UPLOAD_GATE,
    CheckPausedRequest,
    CheckPausedResult,
    ClaimDailySlotRequest,
    ClaimDailySlotResult,
    ClaimOutcome,
    DailyEpisodeInput,
    DailyOutcome,
    EpisodePipelineInput,
    PipelineOptions,
    PipelineOutcome,
    UploadGateRequest,
    UploadGateResult,
    pipeline_workflow_id,
)
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

STAGE_NAMES = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}


@dataclass
class World:
    """fake の子 workflow と Activity が共有する状態（非 sandbox なのでプロセス内で共有できる）。"""

    child_calls: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    #: (workflow 名, episode_id) -> 返す status / "raise"
    overrides: dict[tuple[str, str], str] = field(default_factory=dict)
    paused: bool = False
    uploads_paused: bool = False
    gate_allowed: bool = True
    gate_calls: list[str] = field(default_factory=list)
    #: slot_date -> [(trigger_id, episode_id, started)]
    slots: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    started_episodes: set[str] = field(default_factory=set)
    claim_requests: list[ClaimDailySlotRequest] = field(default_factory=list)
    #: 最初の claim を「commit 後に失敗した」ことにする回数
    claim_fail_after_commit: int = 0


WORLD = World()


def _fake_stage(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    ep = payload["episode_id"]
    WORLD.child_calls.append((name, workflow.info().workflow_id, payload))
    WORLD.started_episodes.add(ep)
    result = WORLD.overrides.get((name, ep), WORLD.overrides.get((name, "*"), STAGE_NAMES[name]))
    if result == "raise":
        raise ApplicationError("boom", non_retryable=True)
    if isinstance(result, str) and result.startswith("raise:"):
        raise ApplicationError(result.removeprefix("raise:"), non_retryable=True)
    return {"episode_id": ep, "status": result}


@workflow.defn(name="ScriptWorkflow")
class FakeScript:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _fake_stage("ScriptWorkflow", payload)


@workflow.defn(name="StoryboardWorkflow")
class FakeStoryboard:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _fake_stage("StoryboardWorkflow", payload)


@workflow.defn(name="ProductionWorkflow")
class FakeProduction:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _fake_stage("ProductionWorkflow", payload)


@workflow.defn(name="RenderWorkflow")
class FakeRender:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _fake_stage("RenderWorkflow", payload)


@workflow.defn(name="UploadWorkflow")
class FakeUpload:
    @workflow.run
    async def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _fake_stage("UploadWorkflow", payload)


FAKE_STAGES = [FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload]


@activity.defn(name=PIPELINE_CHECK_PAUSED)
async def fake_check_paused(req: CheckPausedRequest) -> CheckPausedResult:
    if WORLD.paused:
        return CheckPausedResult(paused=True, reason="paused")
    if req.include_uploads and WORLD.uploads_paused:
        return CheckPausedResult(paused=True, reason="uploads_paused")
    return CheckPausedResult(paused=False)


@activity.defn(name=PIPELINE_CLAIM_DAILY_SLOT)
async def fake_claim(req: ClaimDailySlotRequest) -> ClaimDailySlotResult:
    WORLD.claim_requests.append(req)
    slots = WORLD.slots.setdefault(req.slot_date, [])
    result: ClaimDailySlotResult | None = None
    for trigger, ep in slots:
        if trigger == req.trigger_id:
            result = ClaimDailySlotResult(outcome=ClaimOutcome.EXISTING, episode_id=ep)
    if result is None and len(slots) >= req.daily_limit:
        planned = [ep for _t, ep in slots if ep not in WORLD.started_episodes]
        result = (
            ClaimDailySlotResult(outcome=ClaimOutcome.RESUME, episode_id=planned[0])
            if planned
            else ClaimDailySlotResult(outcome=ClaimOutcome.LIMIT_REACHED)
        )
    if result is None:
        ep = str(uuid.uuid4())
        slots.append((req.trigger_id, ep))
        result = ClaimDailySlotResult(outcome=ClaimOutcome.CREATED, episode_id=ep)
    if WORLD.claim_fail_after_commit > 0:
        WORLD.claim_fail_after_commit -= 1
        raise RuntimeError("connection lost after commit")
    return result


@activity.defn(name=PIPELINE_UPLOAD_GATE)
async def fake_gate(req: UploadGateRequest) -> UploadGateResult:
    WORLD.gate_calls.append(req.episode_id)
    if not WORLD.gate_allowed:
        return UploadGateResult(allowed=False, reason="no final_video", status="render_ready")
    return UploadGateResult(allowed=True, status="render_ready")


@dataclass
class Env:
    client: Client
    queue: str
    stage_queue: str

    def options(self) -> PipelineOptions:
        q = self.stage_queue
        return PipelineOptions(
            script_workflow=("ScriptWorkflow", q),
            storyboard_workflow=("StoryboardWorkflow", q),
            production_workflow=("ProductionWorkflow", q),
            render_workflow=("RenderWorkflow", q),
            upload_workflow=("UploadWorkflow", q),
            pipeline_task_queue=self.queue,
            render_profile_id="long_horizontal",
        )

    async def daily(
        self, *, trigger_id: str, slot_date: str = "2026-09-16", limit: int = 1
    ) -> dict[str, Any]:
        return await self.client.execute_workflow(
            "DailyEpisodeWorkflow",
            DailyEpisodeInput(daily_limit=limit, slot_date=slot_date, options=self.options()),
            id=trigger_id,
            task_queue=self.queue,
            result_type=dict,
        )

    async def pipeline_result(self, episode_id: str) -> dict[str, Any]:
        handle = self.client.get_workflow_handle(pipeline_workflow_id(episode_id), result_type=dict)
        return await handle.result()


@pytest_asyncio.fixture
async def env():
    global WORLD
    WORLD = World()
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue = f"pipeline-test-{uuid.uuid4()}"
        stage_queue = f"stages-test-{uuid.uuid4()}"
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow],
            activities=[fake_check_paused, fake_claim, fake_gate],
        )
        stages = Worker(
            wf_env.client,
            task_queue=stage_queue,
            workflows=FAKE_STAGES,
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, stages:
            yield Env(wf_env.client, queue, stage_queue)


def _stage_names() -> list[str]:
    return [name for name, _id, _p in WORLD.child_calls]


# ------------------------------------------------------------------------- (a) 1 trigger = 1 本


@pytest.mark.asyncio
async def test_daily_trigger_starts_exactly_one_pipeline_and_chains_all_stages(env: Env) -> None:
    result = await env.daily(trigger_id="daily-episode-t1")
    assert result["outcome"] == DailyOutcome.STARTED
    assert result["slot_date"] == "2026-09-16"
    ep = result["episode_id"]
    assert result["pipeline_workflow_id"] == pipeline_workflow_id(ep)

    pipeline = await env.pipeline_result(ep)
    assert pipeline["outcome"] == PipelineOutcome.COMPLETED
    assert pipeline["status"] == "uploaded"
    assert _stage_names() == list(STAGE_NAMES)
    ids = [wid for _n, wid, _p in WORLD.child_calls]
    assert ids == [
        f"episode-{ep}",
        f"episode-{ep}-storyboard",
        f"episode-{ep}-production",
        f"episode-{ep}-render",
        f"episode-{ep}-upload",
    ]
    render_payload = WORLD.child_calls[3][2]
    assert render_payload == {"episode_id": ep, "render_profile_id": "long_horizontal"}
    production_payload = WORLD.child_calls[2][2]
    assert production_payload["episode_id"] == ep
    assert "image_concurrency" in production_payload
    assert WORLD.gate_calls == [ep]
    assert len(WORLD.slots["2026-09-16"]) == 1


# ------------------------------------------------------------ (b)(c) 上限・同日の重複 trigger


@pytest.mark.asyncio
async def test_daily_limit_is_not_exceeded_by_three_triggers_on_the_same_day(env: Env) -> None:
    first = await env.daily(trigger_id="daily-episode-a")
    await env.pipeline_result(first["episode_id"])
    second = await env.daily(trigger_id="daily-episode-b")
    third = await env.daily(trigger_id="daily-episode-c")
    assert second["outcome"] == DailyOutcome.LIMIT_REACHED
    assert third["outcome"] == DailyOutcome.LIMIT_REACHED
    assert second["episode_id"] is None
    assert len(WORLD.slots["2026-09-16"]) == 1
    assert _stage_names().count("ScriptWorkflow") == 1


@pytest.mark.asyncio
async def test_same_trigger_rerun_does_not_double_generate(env: Env) -> None:
    """同じ trigger id の再実行は同じ slot を引き、pipeline を二重に起動しない。"""
    first = await env.daily(trigger_id="daily-episode-dup")
    await env.pipeline_result(first["episode_id"])
    again = await env.daily(trigger_id="daily-episode-dup")
    assert again["episode_id"] == first["episode_id"]
    assert again["outcome"] == DailyOutcome.ALREADY_STARTED
    assert _stage_names().count("ScriptWorkflow") == 1


@pytest.mark.asyncio
async def test_limit_two_allows_two_distinct_episodes(env: Env) -> None:
    a = await env.daily(trigger_id="daily-episode-l1", limit=2)
    b = await env.daily(trigger_id="daily-episode-l2", limit=2)
    c = await env.daily(trigger_id="daily-episode-l3", limit=2)
    assert a["episode_id"] != b["episode_id"]
    assert c["outcome"] == DailyOutcome.LIMIT_REACHED


# ------------------------------------------------------------------------- (d) 再試行・クラッシュ


@pytest.mark.asyncio
async def test_claim_retried_after_commit_does_not_duplicate(env: Env) -> None:
    WORLD.claim_fail_after_commit = 1
    result = await env.daily(trigger_id="daily-episode-retry")
    assert result["outcome"] == DailyOutcome.STARTED
    assert len(WORLD.claim_requests) == 2
    assert len(WORLD.slots["2026-09-16"]) == 1
    await env.pipeline_result(result["episode_id"])
    assert _stage_names().count("ScriptWorkflow") == 1


@pytest.mark.asyncio
async def test_new_trigger_after_crash_before_child_start_resumes_planned_episode(
    env: Env,
) -> None:
    # 前の trigger が claim を commit した直後に落ち、子を起動しなかった状況を作る
    WORLD.slots["2026-09-16"] = [("daily-episode-crashed", "ep-planned")]
    result = await env.daily(trigger_id="daily-episode-next")
    assert result["outcome"] == DailyOutcome.STARTED
    assert result["episode_id"] == "ep-planned"
    pipeline = await env.pipeline_result("ep-planned")
    assert pipeline["outcome"] == PipelineOutcome.COMPLETED
    assert len(WORLD.slots["2026-09-16"]) == 1


# ------------------------------------------------------------- (e) blocked は翌日を止めない


@pytest.mark.asyncio
async def test_blocked_episode_does_not_stop_the_next_day(env: Env) -> None:
    WORLD.overrides[("StoryboardWorkflow", "*")] = "blocked"
    day1 = await env.daily(trigger_id="daily-episode-d1", slot_date="2026-09-16")
    ep1 = day1["episode_id"]
    p1 = await env.pipeline_result(ep1)
    assert p1["outcome"] == PipelineOutcome.STOPPED
    assert p1["status"] == "blocked"

    del WORLD.overrides[("StoryboardWorkflow", "*")]
    day2 = await env.daily(trigger_id="daily-episode-d2", slot_date="2026-09-17")
    assert day2["outcome"] == DailyOutcome.STARTED
    assert day2["episode_id"] != ep1
    p2 = await env.pipeline_result(day2["episode_id"])
    assert p2["outcome"] == PipelineOutcome.COMPLETED


# ------------------------------------------------------------------------- (f) 停止条件


async def _run_pipeline(env: Env, ep: str) -> dict[str, Any]:
    return await env.client.execute_workflow(
        "EpisodePipelineWorkflow",
        EpisodePipelineInput(episode_id=ep, options=env.options()),
        id=pipeline_workflow_id(ep),
        task_queue=env.queue,
        result_type=dict,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "status", "expected_calls"),
    [
        ("ScriptWorkflow", "needs_work", ["ScriptWorkflow"]),
        ("StoryboardWorkflow", "blocked", ["ScriptWorkflow", "StoryboardWorkflow"]),
        (
            "ProductionWorkflow",
            "blocked",
            ["ScriptWorkflow", "StoryboardWorkflow", "ProductionWorkflow"],
        ),
        (
            "RenderWorkflow",
            "raise",
            ["ScriptWorkflow", "StoryboardWorkflow", "ProductionWorkflow", "RenderWorkflow"],
        ),
    ],
)
async def test_pipeline_stops_at_first_non_parking_status(
    env: Env, stage: str, status: str, expected_calls: list[str]
) -> None:
    ep = f"ep-{uuid.uuid4()}"
    WORLD.overrides[(stage, ep)] = status
    result = await _run_pipeline(env, ep)
    assert result["outcome"] == PipelineOutcome.STOPPED
    assert result["stopped_stage"] == stage.removesuffix("Workflow").lower()
    assert result["reason"]
    assert _stage_names() == expected_calls
    assert WORLD.gate_calls == []
    if status != "raise":
        assert result["status"] == status


@pytest.mark.asyncio
async def test_child_failure_reason_does_not_copy_raw_error_text_into_history(env: Env) -> None:
    """子の例外文には session URI 等が混ざりうる。pipeline の結果（Temporal 履歴）へ写さない。"""
    ep = f"ep-{uuid.uuid4()}"
    WORLD.overrides[("RenderWorkflow", ep)] = "raise:https://upload.example/?upload_id=SECRET-XYZ"
    result = await _run_pipeline(env, ep)
    assert result["outcome"] == PipelineOutcome.STOPPED
    assert result["stopped_stage"] == "render"
    assert "SECRET-XYZ" not in result["reason"]
    assert "ApplicationError" in result["reason"]


@pytest.mark.asyncio
async def test_upload_failure_status_is_reported_as_stopped(env: Env) -> None:
    ep = f"ep-{uuid.uuid4()}"
    WORLD.overrides[("UploadWorkflow", ep)] = "blocked"
    result = await _run_pipeline(env, ep)
    assert result["outcome"] == PipelineOutcome.STOPPED
    assert result["stopped_stage"] == "upload"
    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_child_already_running_stops_gracefully(env: Env) -> None:
    ep = f"ep-{uuid.uuid4()}"

    # 同じ id の storyboard を先に（完了しない形で）起動しておく: 別 queue に投げて誰も拾わない
    await env.client.start_workflow(
        "StoryboardWorkflow",
        {"episode_id": ep},
        id=f"episode-{ep}-storyboard",
        task_queue=f"nobody-{uuid.uuid4()}",
    )
    result = await _run_pipeline(env, ep)
    assert result["outcome"] == PipelineOutcome.STOPPED
    assert result["stopped_stage"] == "storyboard"
    assert "already" in result["reason"]
    assert _stage_names() == ["ScriptWorkflow"]


# ------------------------------------------------------------------------- (g) 投稿ゲート


@pytest.mark.asyncio
async def test_upload_gate_refusal_skips_upload(env: Env) -> None:
    WORLD.gate_allowed = False
    ep = f"ep-{uuid.uuid4()}"
    result = await _run_pipeline(env, ep)
    assert result["outcome"] == PipelineOutcome.UPLOAD_SKIPPED
    assert result["stopped_stage"] == "upload"
    assert result["reason"] == "no final_video"
    assert result["status"] == "render_ready"
    assert "UploadWorkflow" not in _stage_names()


# ------------------------------------------------------------------------- PAUSED


@pytest.mark.asyncio
async def test_paused_switch_skips_the_daily_run_without_claiming(env: Env) -> None:
    WORLD.paused = True
    result = await env.daily(trigger_id="daily-episode-paused")
    assert result["outcome"] == DailyOutcome.PAUSED
    assert WORLD.claim_requests == []
    assert WORLD.child_calls == []


@pytest.mark.asyncio
async def test_slot_date_defaults_to_local_date_of_start_time(env: Env) -> None:
    result = await env.client.execute_workflow(
        "DailyEpisodeWorkflow",
        DailyEpisodeInput(options=env.options(), timezone="Asia/Tokyo"),
        id="daily-episode-now",
        task_queue=env.queue,
        result_type=dict,
    )
    assert len(result["slot_date"]) == 10
    assert WORLD.claim_requests[0].slot_date == result["slot_date"]
    assert WORLD.claim_requests[0].trigger_id == "daily-episode-now"


@pytest.mark.asyncio
async def test_invalid_timezone_fails_the_trigger_not_silently(env: Env) -> None:
    with pytest.raises(WorkflowFailureError):
        await env.client.execute_workflow(
            "DailyEpisodeWorkflow",
            DailyEpisodeInput(options=env.options(), timezone="Nowhere/Invalid"),
            id="daily-episode-badtz",
            task_queue=env.queue,
            result_type=dict,
        )
