"""収集 worker の起動条件（INV-159）と Workflow / Activity（INV-152）。時間スキップの Temporal。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from contracts.bgm_acquisition_activities import (
    BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY,
    BGM_ACQUISITION_MAX_ATTEMPTS,
    BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW,
    BGM_ACQUISITION_RUN_DEMAND_ACTIVITY,
    BGM_ACQUISITION_WORKFLOW_NAME,
    BgmAcquisitionInput,
    BgmAcquisitionOutput,
    DemandRef,
    DemandRunResult,
    ListDemandsRequest,
)
from contracts.music_sources import ACQUISITION_RUN_TIMEOUT_SECONDS
from infrastructure.config import Settings
from workers.bgm_acquisition.activities import BgmAcquisitionActivities, summarize
from workers.bgm_acquisition.run_worker import build_user_agent, require_enabled
from workers.bgm_acquisition.workflows import (
    RUN_RETRY_POLICY,
    RUN_START_TO_CLOSE,
    BgmAcquisitionWorkflow,
)


def _settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **kw)  # type: ignore[call-arg]


# ------------------------------------------------------------------ 起動条件


@pytest.mark.parametrize(
    "kw",
    [{}, {"bgm_acquisition_enabled": True}, {"bgm_enabled": True}],
    ids=["both-off", "acq-only", "bgm-only"],
)
def test_worker_refuses_to_start_unless_both_flags_are_on(kw: dict) -> None:
    with pytest.raises(SystemExit) as exc:
        require_enabled(_settings(**kw))
    assert "BGM_ACQUISITION_ENABLED" in str(exc.value)


def test_worker_refuses_to_start_when_the_gate_is_not_enforced() -> None:
    # INV-159 の鎖: ACQUISITION → BGM → AVP_QUALITY_GATE_ENFORCE（ADR-0087 §10）
    with pytest.raises(SystemExit) as exc:
        require_enabled(_settings(bgm_acquisition_enabled=True, bgm_enabled=True))
    assert "AVP_QUALITY_GATE_ENFORCE" in str(exc.value)


def test_worker_starts_when_the_whole_flag_chain_is_on() -> None:
    require_enabled(
        _settings(bgm_acquisition_enabled=True, bgm_enabled=True, AVP_QUALITY_GATE_ENFORCE=True)
    )


@pytest.mark.parametrize("contact", [None, "", "   "])
def test_contact_is_required_for_the_user_agent(contact: str | None) -> None:
    with pytest.raises(SystemExit):
        build_user_agent(contact)


def test_user_agent_satisfies_the_wikimedia_policy() -> None:
    from infrastructure.music.sources.commons import validate_user_agent

    ua = build_user_agent("ops@example.invalid")
    assert validate_user_agent(ua) == ua and "avp2-bgm-acquisition" in ua


def test_timeouts_and_retries_are_bounded() -> None:
    assert RUN_RETRY_POLICY.maximum_attempts == BGM_ACQUISITION_MAX_ATTEMPTS <= 2
    assert timedelta(seconds=ACQUISITION_RUN_TIMEOUT_SECONDS) <= RUN_START_TO_CLOSE
    assert timedelta(seconds=ACQUISITION_RUN_TIMEOUT_SECONDS + 300) >= RUN_START_TO_CLOSE


# ------------------------------------------------------------------ Activity 層


class _Repo:
    async def pending_demands(self, *, limit: int):
        return tuple((f"{i:064x}", {"mood": "calm"}) for i in range(limit + 2))[:limit]


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def run(self, demand: str, requirements: Any):
        self.calls.append((demand, dict(requirements)))
        return None


async def test_activity_layer_lists_and_runs_without_touching_a_network() -> None:
    svc = _Service()
    acts = BgmAcquisitionActivities(repo=_Repo(), service=svc)  # type: ignore[arg-type]
    demands = await acts.list_demands(ListDemandsRequest(limit=2))
    assert [d.demand_sha256 for d in demands] == [f"{0:064x}", f"{1:064x}"]
    result = await acts.run_demand(demands[0])
    assert result.status == "rate_limited" and svc.calls == [
        (demands[0].demand_sha256, {"mood": "calm"})
    ]
    assert summarize("a" * 64, None).status == "rate_limited"


# ------------------------------------------------------------------ Workflow


@pytest_asyncio.fixture
async def env():
    environment = await WorkflowEnvironment.start_time_skipping()
    try:
        yield environment
    finally:
        await environment.shutdown()


def _mocks(n_demands: int, fail_on: set[str] | None = None):
    ran: list[str] = []
    attempts: dict[str, int] = {}

    @activity.defn(name=BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY)
    async def list_demands(req: ListDemandsRequest) -> list[DemandRef]:
        return [
            DemandRef(demand_sha256=f"{i:064x}", requirements={"mood": "calm"})
            for i in range(n_demands)
        ][: req.limit + 5]

    @activity.defn(name=BGM_ACQUISITION_RUN_DEMAND_ACTIVITY)
    async def run_demand(d: DemandRef) -> DemandRunResult:
        attempts[d.demand_sha256] = attempts.get(d.demand_sha256, 0) + 1
        if fail_on and d.demand_sha256 in fail_on:
            raise ApplicationError("boom", type="RuntimeError")
        ran.append(d.demand_sha256)
        return DemandRunResult(d.demand_sha256, "ran", registered_approved=1)

    return [list_demands, run_demand], ran, attempts


async def _run(
    env: WorkflowEnvironment, acts: list, inp: BgmAcquisitionInput | None = None
) -> BgmAcquisitionOutput:
    queue = f"bgm-acq-test-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client, task_queue=queue, workflows=[BgmAcquisitionWorkflow], activities=acts
    ):
        return await env.client.execute_workflow(
            BGM_ACQUISITION_WORKFLOW_NAME,
            inp or BgmAcquisitionInput(),
            id=f"bgm-acq-{uuid.uuid4().hex[:8]}",
            task_queue=queue,
            result_type=BgmAcquisitionOutput,
        )


async def test_workflow_processes_demands_sequentially_and_bounded(
    env: WorkflowEnvironment,
) -> None:
    acts, ran, _ = _mocks(10)
    out = await _run(env, acts)
    assert len(ran) == BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW == len(out.results)
    assert ran == [f"{i:064x}" for i in range(BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW)]


async def test_workflow_input_cannot_exceed_the_bound(env: WorkflowEnvironment) -> None:
    acts, ran, _ = _mocks(10)
    await _run(env, acts, BgmAcquisitionInput(max_demands=99))
    assert len(ran) == BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW
    acts2, ran2, _ = _mocks(10)
    out = await _run(env, acts2, BgmAcquisitionInput(max_demands=0))
    assert ran2 == [] and out.results == ()


async def test_one_failing_demand_does_not_stop_the_others_and_retries_are_bounded(
    env: WorkflowEnvironment,
) -> None:
    bad = f"{1:064x}"
    acts, ran, attempts = _mocks(3, fail_on={bad})
    out = await _run(env, acts)
    statuses = {r.demand_sha256: r.status for r in out.results}
    assert (
        statuses[bad] == "failed"
        and statuses[f"{0:064x}"] == "ran"
        and statuses[f"{2:064x}"] == "ran"
    )
    assert attempts[bad] == BGM_ACQUISITION_MAX_ATTEMPTS


async def test_empty_queue_is_a_noop(env: WorkflowEnvironment) -> None:
    acts, ran, _ = _mocks(0)
    out = await _run(env, acts)
    assert out.results == () and ran == []
