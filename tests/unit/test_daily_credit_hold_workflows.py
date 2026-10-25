"""日次の入口ゲート・保留・Codex 事前診断の編成（ADR-0058 / INV-75・INV-76・INV-78）。

time-skipping の Temporal テスト環境。工程の子 workflow・状態系 Activity は fake、**ゲートの
Activity は本物**（``PipelineActivities.daily_gate`` / ``production_gate``）で、breaker・保留は
fake の port（``contracts.provider_health`` の Protocol）。fal・Codex・YouTube は呼ばない。
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from temporalio import activity, workflow
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, UnsandboxedWorkflowRunner, Worker

from contracts.codex_models import (
    CODEX_MODEL_PREFLIGHT_ACTIVITY,
    CodexModelVerdict,
    CodexPreflightRequest,
    CodexPreflightResult,
)
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
from contracts.provider_health import (
    MAX_HELD_EPISODES,
    BreakerSnapshot,
    BreakerState,
    HoldReason,
    ProviderAccount,
    RefusalKind,
)
from contracts.topic_planning import TopicPlannerInput, TopicPlannerResult
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.gate_ports import GatePorts
from workers.pipeline.workflows import DailyEpisodeWorkflow, EpisodePipelineWorkflow

STAGE_STATUS = {
    "ScriptWorkflow": "script_ready",
    "StoryboardWorkflow": "storyboard_ready",
    "ProductionWorkflow": "assets_ready",
    "RenderWorkflow": "render_ready",
    "UploadWorkflow": "uploaded",
}


@dataclass
class Hold:
    episode_id: str
    scene_id: str | None
    reason: HoldReason
    resumable_stage: str
    account_key: str
    detail: dict[str, str] = field(default_factory=dict)


@dataclass
class FakeProviderHealth:
    """``ProviderHealthReader`` + ``CreditHoldWriter``。保留の件数は書かれた保留から導く。"""

    state: BreakerState = BreakerState.CLOSED
    refusal: RefusalKind | None = RefusalKind.BALANCE_EXHAUSTED
    holds: list[Hold] = field(default_factory=list)
    extra_open_holds: int = 0

    async def snapshot(self, account: ProviderAccount) -> BreakerSnapshot:
        return BreakerSnapshot(
            account_key=account.key,
            state=self.state,
            reason=self.refusal if self.state is not BreakerState.CLOSED else None,
            opened_at=datetime(2026, 10, 25, tzinfo=UTC),
        )

    async def open_hold_count(self) -> int:
        return len({h.episode_id for h in self.holds}) + self.extra_open_holds

    async def hold_episode(
        self,
        *,
        episode_id: str,
        scene_id: str | None,
        account: ProviderAccount,
        reason: HoldReason,
        resumable_stage: str,
        generation_profile_id: str | None,
        detail: Mapping[str, str] | None = None,
    ) -> None:
        if any(h.episode_id == episode_id and h.scene_id == scene_id for h in self.holds):
            return  # 冪等
        self.holds.append(
            Hold(episode_id, scene_id, reason, resumable_stage, account.key, dict(detail or {}))
        )


@dataclass
class FakeUnreconciled:
    n: int = 0

    async def count(self) -> int:
        return self.n


@dataclass
class World:
    health: FakeProviderHealth = field(default_factory=FakeProviderHealth)
    unreconciled: FakeUnreconciled = field(default_factory=FakeUnreconciled)
    child_calls: list[tuple[str, str]] = field(default_factory=list)
    planner_days: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    slots: dict[str, str] = field(default_factory=dict)
    codex_verdict: CodexModelVerdict = CodexModelVerdict.UNVERIFIED
    codex_blocking: bool = False
    codex_calls: int = 0


WORLD = World()


def _fake_stage(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    WORLD.child_calls.append((name, payload["episode_id"]))
    return {"episode_id": payload["episode_id"], "status": STAGE_STATUS[name]}


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


@workflow.defn(name="TopicPlannerWorkflow")
class FakeTopicPlanner:
    @workflow.run
    async def run(self, req: TopicPlannerInput) -> TopicPlannerResult:
        WORLD.planner_days.append(req.plan_date)
        return TopicPlannerResult(
            topic_plan_id=str(uuid.uuid4()),
            topic=f"topic {req.plan_date}",
            reused=False,
            analytics_mode="no_analytics",
        )


FAKE_STAGES = [FakeScript, FakeStoryboard, FakeProduction, FakeRender, FakeUpload, FakeTopicPlanner]


@activity.defn(name=PIPELINE_CHECK_PAUSED)
async def fake_check_paused(req: CheckPausedRequest) -> CheckPausedResult:
    return CheckPausedResult(paused=False)


@activity.defn(name=PIPELINE_CLAIM_DAILY_SLOT)
async def fake_claim(req: ClaimDailySlotRequest) -> ClaimDailySlotResult:
    WORLD.claims.append(req.slot_date)
    ep = WORLD.slots.setdefault(req.slot_date, str(uuid.uuid4()))
    return ClaimDailySlotResult(
        outcome=ClaimOutcome.CREATED, episode_id=ep, topic_plan_id=req.topic_plan_id
    )


@activity.defn(name=PIPELINE_UPLOAD_GATE)
async def fake_upload_gate(req: UploadGateRequest) -> UploadGateResult:
    return UploadGateResult(allowed=True, status="render_ready")


@activity.defn(name=CODEX_MODEL_PREFLIGHT_ACTIVITY)
async def fake_codex_preflight(req: CodexPreflightRequest) -> CodexPreflightResult:
    WORLD.codex_calls += 1
    return CodexPreflightResult(
        verdict=WORLD.codex_verdict.value,
        blocking=WORLD.codex_blocking,
        model="gpt-6-sol",
        source="config.toml",
        message="fake preflight",
    )


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
            topic_planner_workflow=("TopicPlannerWorkflow", q),
            pipeline_task_queue=self.queue,
            render_profile_id="long_horizontal",
        )

    async def daily(self, day: int) -> dict[str, Any]:
        slot = f"2026-10-{day:02d}"
        return await self.client.execute_workflow(
            "DailyEpisodeWorkflow",
            DailyEpisodeInput(slot_date=slot, options=self.options()),
            id=f"daily-episode-{slot}",
            task_queue=self.queue,
            result_type=dict,
        )

    async def pipeline(self, episode_id: str) -> dict[str, Any]:
        handle = self.client.get_workflow_handle(pipeline_workflow_id(episode_id), result_type=dict)
        return await handle.result()


@pytest_asyncio.fixture
async def env():
    global WORLD
    WORLD = World()
    acts = PipelineActivities(
        session_factory=cast(Any, None),
        paused_env=False,
        uploads_paused_env=False,
        gate_ports=GatePorts(
            health=WORLD.health, holds=WORLD.health, unreconciled=WORLD.unreconciled
        ),
    )
    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
        queue = f"pipeline-hold-{uuid.uuid4()}"
        stage_queue = f"stages-hold-{uuid.uuid4()}"
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow],
            activities=[
                fake_check_paused,
                fake_claim,
                fake_upload_gate,
                acts.daily_gate,
                acts.production_gate,
            ],
        )
        stages = Worker(
            wf_env.client,
            task_queue=stage_queue,
            workflows=FAKE_STAGES,
            activities=[fake_codex_preflight],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        async with main, stages:
            yield Env(wf_env.client, queue, stage_queue)


def _stage_names() -> list[str]:
    return [n for n, _ in WORLD.child_calls]


# --------------------------------------------------------------------- breaker が開いている


@pytest.mark.asyncio
async def test_open_breaker_keeps_planning_and_script_but_starts_no_paid_work(env: Env) -> None:
    WORLD.health.state = BreakerState.OPEN
    daily = await env.daily(1)
    assert daily["outcome"] == DailyOutcome.STARTED
    pipeline = await env.pipeline(daily["episode_id"])

    assert WORLD.planner_days == ["2026-10-01"]
    assert _stage_names() == ["ScriptWorkflow", "StoryboardWorkflow"]  # production 以降は無い
    assert pipeline["outcome"] == PipelineOutcome.HELD
    assert pipeline["outcome"] != PipelineOutcome.COMPLETED  # 保留は成功ではない
    assert pipeline["stopped_stage"] == "production"
    assert pipeline["status"] == "storyboard_ready"
    assert [
        (h.episode_id, h.scene_id, h.reason, h.resumable_stage) for h in WORLD.health.holds
    ] == [(daily["episode_id"], None, HoldReason.WAITING_FOR_CREDITS, "production")]


@pytest.mark.asyncio
async def test_an_unconfirmed_refusal_is_not_called_waiting_for_credits(env: Env) -> None:
    """fal の 403 のように原因が確定できないものを、残高不足と断定しない。"""
    WORLD.health.state = BreakerState.OPEN
    WORLD.health.refusal = RefusalKind.UNKNOWN
    daily = await env.daily(1)
    await env.pipeline(daily["episode_id"])
    assert [h.reason for h in WORLD.health.holds] == [HoldReason.PROVIDER_REFUSAL_UNKNOWN]


@pytest.mark.asyncio
async def test_half_open_breaker_is_not_used_by_the_daily_run(env: Env) -> None:
    """所有者承認の上限付き再開の枠は、日次が使わない。"""
    WORLD.health.state = BreakerState.HALF_OPEN
    daily = await env.daily(1)
    pipeline = await env.pipeline(daily["episode_id"])
    assert pipeline["outcome"] == PipelineOutcome.HELD
    assert "ProductionWorkflow" not in _stage_names()


@pytest.mark.asyncio
async def test_closed_breaker_runs_the_whole_pipeline_as_before(env: Env) -> None:
    daily = await env.daily(1)
    pipeline = await env.pipeline(daily["episode_id"])
    assert pipeline["outcome"] == PipelineOutcome.COMPLETED
    assert _stage_names() == list(STAGE_STATUS)
    assert WORLD.health.holds == []


@pytest.mark.asyncio
async def test_a_pipeline_not_started_by_daily_is_never_stopped_by_the_gate(env: Env) -> None:
    """API / 統一再開が起動する pipeline（credit_gate 既定 False）は、所有者の再開を妨げない。"""
    WORLD.health.state = BreakerState.OPEN
    ep = str(uuid.uuid4())
    result = await env.client.execute_workflow(
        "EpisodePipelineWorkflow",
        EpisodePipelineInput(episode_id=ep, options=env.options(), start_stage="production"),
        id=pipeline_workflow_id(ep),
        task_queue=env.queue,
        result_type=dict,
    )
    assert result["outcome"] == PipelineOutcome.COMPLETED
    assert "ProductionWorkflow" in _stage_names()
    assert WORLD.health.holds == []


# ------------------------------------------------------------------------------ 保留の上限


@pytest.mark.asyncio
async def test_backlog_at_the_cap_creates_no_episode_and_calls_no_codex(env: Env) -> None:
    WORLD.health.state = BreakerState.OPEN
    WORLD.health.extra_open_holds = MAX_HELD_EPISODES
    daily = await env.daily(1)
    assert daily["outcome"] == DailyOutcome.HELD_BACKLOG
    assert daily["outcome"] != DailyOutcome.STARTED
    assert "MAX_HELD_EPISODES" in daily["reason"]
    assert WORLD.planner_days == [] and WORLD.claims == [] and WORLD.child_calls == []
    assert WORLD.codex_calls == 0


@pytest.mark.asyncio
async def test_seven_days_of_open_breaker_are_bounded_and_repeat_no_paid_attempt(env: Env) -> None:
    """INV-76 / INV-78: breaker が開いたまま 7 日。Episode は高々上限、有料呼び出しは 0。"""
    WORLD.health.state = BreakerState.OPEN
    outcomes = []
    for day in range(1, 8):
        daily = await env.daily(day)
        outcomes.append(daily["outcome"])
        if daily["episode_id"]:
            await env.pipeline(daily["episode_id"])

    assert outcomes == [DailyOutcome.STARTED] * MAX_HELD_EPISODES + [DailyOutcome.HELD_BACKLOG] * (
        7 - MAX_HELD_EPISODES
    )
    assert len(WORLD.slots) == MAX_HELD_EPISODES
    assert len(WORLD.planner_days) == MAX_HELD_EPISODES  # Codex は上限までしか使わない
    assert "ProductionWorkflow" not in _stage_names()  # 有料呼び出しの再試行は無い
    # 日次は過去の Episode を再開しない: 各 Episode の script / storyboard は 1 回ずつ
    assert _stage_names().count("ScriptWorkflow") == MAX_HELD_EPISODES
    assert _stage_names().count("StoryboardWorkflow") == MAX_HELD_EPISODES
    assert len({h.episode_id for h in WORLD.health.holds}) == MAX_HELD_EPISODES


@pytest.mark.asyncio
async def test_unreconciled_reservations_are_reported_but_do_not_stop_a_new_episode(
    env: Env,
) -> None:
    WORLD.unreconciled.n = 3
    daily = await env.daily(1)
    assert daily["outcome"] == DailyOutcome.STARTED


# ----------------------------------------------------------------------- Codex の事前診断


@pytest.mark.asyncio
async def test_a_blocking_codex_preflight_fails_the_daily_run_before_any_codex_call(
    env: Env,
) -> None:
    WORLD.codex_verdict = CodexModelVerdict.NOT_IN_CATALOG
    WORLD.codex_blocking = True
    with pytest.raises(WorkflowFailureError) as err:
        await env.daily(1)
    cause = err.value.cause
    assert isinstance(cause, ApplicationError)
    assert cause.type == "CodexModelPreflightFailed"
    assert "not_in_catalog" in cause.message
    assert WORLD.planner_days == [] and WORLD.claims == []


@pytest.mark.asyncio
async def test_an_unverified_codex_model_does_not_stop_the_run(env: Env) -> None:
    """UNVERIFIED は「使える証明が無い」だけで、止める根拠ではない。"""
    daily = await env.daily(1)
    assert daily["outcome"] == DailyOutcome.STARTED
    assert WORLD.codex_calls == 1


# ------------------------------------------------------- ゲート Activity 自体（結線ミスの扱い）


def _acts(ports: GatePorts) -> PipelineActivities:
    return PipelineActivities(
        session_factory=cast(Any, None),
        paused_env=False,
        uploads_paused_env=False,
        gate_ports=ports,
    )


@pytest.mark.asyncio
async def test_unwired_ports_behave_as_before_and_say_so() -> None:
    acts = _acts(GatePorts())
    from contracts.daily_gate import DailyGateRequest, ProductionGateRequest

    gate = await acts.daily_gate(DailyGateRequest(slot_date="2026-10-01"))
    assert gate.mode == "normal" and gate.breaker_state == "closed"
    assert (await acts.production_gate(ProductionGateRequest(episode_id="e"))).allowed


@pytest.mark.asyncio
async def test_open_breaker_without_a_hold_writer_refuses_instead_of_going_on_to_paid_work() -> (
    None
):
    from contracts.daily_gate import ProductionGateRequest

    health = FakeProviderHealth(state=BreakerState.OPEN)
    acts = _acts(GatePorts(health=health, holds=None))
    with pytest.raises(RuntimeError, match="CreditHoldWriter"):
        await acts.production_gate(ProductionGateRequest(episode_id="e"))


# ------------------------------------------- 旧コード（ADR-0058 より前）の履歴の replay（INV-78）

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    ["daily_episode_pre_0058_history.json", "episode_pipeline_pre_0058_history.json"],
)
async def test_histories_recorded_before_adr_0058_replay_without_nondeterminism(name: str) -> None:
    """fixture は 40cf2ca（ADR-0058 の前）の workflow を fake と走らせて取得した履歴。

    入口ゲート・Codex 事前診断は ``workflow.patched``、production ゲートは入力フラグ
    （旧履歴は False）で分岐するので、旧履歴は旧経路で replay できる。
    """
    history = WorkflowHistory.from_json(f"replay-{name}", (_FIXTURES / name).read_text())
    await Replayer(workflows=[DailyEpisodeWorkflow, EpisodePipelineWorkflow]).replay_workflow(
        history
    )
