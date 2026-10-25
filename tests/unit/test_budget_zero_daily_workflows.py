"""予算 0 / 未設定でも日次は失敗せず、有料生成を 0 回のまま正常終了する（ADR-0061 / INV-84）。

``test_daily_credit_hold_workflows.py`` の time-skipping Temporal 環境・fake 工程・本物のゲート
Activity をそのまま使い、breaker は **closed のまま**（fal へは 1 回も行っていない）で、
ローカル予算だけが保留の理由になることを確かめる。fal・Codex・YouTube は呼ばない。
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
import pytest_asyncio
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

import tests.unit.test_daily_credit_hold_workflows as base
from contracts.daily_gate import DailyGateRequest, ProductionGateRequest
from contracts.pipeline import DailyOutcome, PipelineOutcome
from contracts.provider_health import MAX_HELD_EPISODES, BreakerState, BudgetStatus, HoldReason
from domain.budget_gate import evaluate_budget
from workers.pipeline.activities import PipelineActivities
from workers.pipeline.gate_ports import GatePorts


@pytest_asyncio.fixture
async def make_env():
    """予算を指定して環境を組む。fixture 内の with を閉じるため async generator にする。"""
    stack: list[Any] = []

    async def _make(budget_usd: Decimal | None, **ports: Any) -> base.Env:
        base.WORLD = base.World()
        acts = PipelineActivities(
            session_factory=cast(Any, None),
            paused_env=False,
            uploads_paused_env=False,
            gate_ports=GatePorts(
                health=base.WORLD.health,
                holds=base.WORLD.health,
                unreconciled=base.WORLD.unreconciled,
                budget=evaluate_budget(budget_usd),
                **ports,
            ),
        )
        wf_env = await WorkflowEnvironment.start_time_skipping()
        stack.append(wf_env)
        queue, stage_queue = f"budget-{uuid.uuid4()}", f"budget-stages-{uuid.uuid4()}"
        main = Worker(
            wf_env.client,
            task_queue=queue,
            workflows=[base.DailyEpisodeWorkflow, base.EpisodePipelineWorkflow],
            activities=[
                base.fake_check_paused,
                base.fake_claim,
                base.fake_upload_gate,
                acts.daily_gate,
                acts.production_gate,
            ],
        )
        stages = Worker(
            wf_env.client,
            task_queue=stage_queue,
            workflows=base.FAKE_STAGES,
            activities=[base.fake_codex_preflight],
            workflow_runner=UnsandboxedWorkflowRunner(),
        )
        await main.__aenter__()
        await stages.__aenter__()
        stack.extend([main, stages])
        return base.Env(wf_env.client, queue, stage_queue)

    yield _make
    for item in reversed(stack):
        if isinstance(item, Worker):
            await item.__aexit__(None, None, None)
        else:
            await item.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("budget_usd", "decision"),
    [(Decimal("0"), "budget_disabled"), (None, "budget_unset")],
)
async def test_zero_or_unset_budget_plans_scripts_then_holds_and_ends_normally(
    make_env, budget_usd, decision
) -> None:
    env = await make_env(budget_usd)
    daily = await env.daily(1)
    assert daily["outcome"] == DailyOutcome.STARTED  # Daily は正常終了（例外にならない）
    pipeline = await env.pipeline(daily["episode_id"])

    assert base.WORLD.planner_days == ["2026-10-01"]  # 企画（無課金）は走る
    assert base._stage_names() == ["ScriptWorkflow", "StoryboardWorkflow"]  # 有料工程は始まらない
    assert pipeline["outcome"] == PipelineOutcome.HELD  # 失敗でも完成でもない
    assert pipeline["outcome"] != PipelineOutcome.COMPLETED
    assert pipeline["stopped_stage"] == "production"
    assert base.WORLD.health.state is BreakerState.CLOSED  # fal へ行っていない
    assert [(h.episode_id, h.scene_id, h.reason) for h in base.WORLD.health.holds] == [
        (daily["episode_id"], None, HoldReason.WAITING_FOR_CREDITS)
    ]
    assert base.WORLD.health.holds[0].detail["decision_reason"] == decision


@pytest.mark.asyncio
async def test_positive_budget_runs_the_whole_pipeline_as_before(make_env) -> None:
    env = await make_env(Decimal("25"))
    daily = await env.daily(1)
    pipeline = await env.pipeline(daily["episode_id"])
    assert pipeline["outcome"] == PipelineOutcome.COMPLETED
    assert base._stage_names() == list(base.STAGE_STATUS)
    assert base.WORLD.health.holds == []


@pytest.mark.asyncio
async def test_a_month_of_zero_budget_never_fails_and_is_bounded(make_env) -> None:
    """INV-84: 予算 0 が何週間続いても、毎朝の実行は失敗として溜まらず、有料呼び出しは 0。"""
    env = await make_env(Decimal("0"))
    outcomes = []
    for day in range(1, 29):
        daily = await env.daily(day)  # 例外が出れば（= Daily が失敗なら）ここで落ちる
        outcomes.append(daily["outcome"])
        if daily["episode_id"]:
            pipeline = await env.pipeline(daily["episode_id"])
            assert pipeline["outcome"] == PipelineOutcome.HELD

    assert outcomes == [DailyOutcome.STARTED] * MAX_HELD_EPISODES + [DailyOutcome.HELD_BACKLOG] * (
        28 - MAX_HELD_EPISODES
    )
    assert len(base.WORLD.slots) == MAX_HELD_EPISODES  # 3 日目以降は Episode を作らない
    assert len(base.WORLD.planner_days) == MAX_HELD_EPISODES  # Codex も無駄に使わない
    assert "ProductionWorkflow" not in base._stage_names()
    assert base.WORLD.health.state is BreakerState.CLOSED


@pytest.mark.asyncio
async def test_held_backlog_reason_and_result_are_machine_readable(make_env) -> None:
    env = await make_env(Decimal("0"))
    for day in (1, 2):
        daily = await env.daily(day)
        await env.pipeline(daily["episode_id"])
    third = await env.daily(3)
    assert third["outcome"] == DailyOutcome.HELD_BACKLOG
    assert "MAX_HELD_EPISODES" in third["reason"]
    assert third["episode_id"] in (None, "")


async def test_gate_activity_results_carry_the_budget_status() -> None:
    acts = PipelineActivities(
        session_factory=cast(Any, None),
        paused_env=False,
        uploads_paused_env=False,
        gate_ports=GatePorts(
            health=base.FakeProviderHealth(),
            holds=base.FakeProviderHealth(),
            budget=evaluate_budget(Decimal("0")),
        ),
    )
    gate = await acts.daily_gate(DailyGateRequest(slot_date="2026-10-01"))
    assert gate.mode == "paid_held"
    assert gate.budget_status == BudgetStatus.DISABLED.value
    assert gate.breaker_state == "closed"
    assert "budget_disabled" in gate.reasons

    prod = await acts.production_gate(ProductionGateRequest(episode_id="e"))
    assert prod.allowed is False
    assert prod.hold_reason == HoldReason.WAITING_FOR_CREDITS.value


async def test_budget_hold_without_a_writer_refuses_rather_than_going_on_to_paid_work() -> None:
    acts = PipelineActivities(
        session_factory=cast(Any, None),
        paused_env=False,
        uploads_paused_env=False,
        gate_ports=GatePorts(budget=evaluate_budget(Decimal("0"))),
    )
    with pytest.raises(RuntimeError, match="CreditHoldWriter"):
        await acts.production_gate(ProductionGateRequest(episode_id="e"))


async def test_hold_is_idempotent_when_the_production_gate_is_called_twice() -> None:
    health = base.FakeProviderHealth()
    acts = PipelineActivities(
        session_factory=cast(Any, None),
        paused_env=False,
        uploads_paused_env=False,
        gate_ports=GatePorts(health=health, holds=health, budget=evaluate_budget(None)),
    )
    for _ in range(3):
        await acts.production_gate(ProductionGateRequest(episode_id="e"))
    assert len(health.holds) == 1
    assert health.holds[0].reason is HoldReason.WAITING_FOR_CREDITS
