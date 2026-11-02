"""BgmAcquisitionWorkflow（ADR-0087 §3、INV-152）。

決定的（I/O・時計・乱数・DB を使わない）。需要の一覧を 1 回読み、上限件数までを**逐次**に 1 件ずつ
Activity へ渡す。1 件の失敗は他の需要を止めない。Episode のワークフローとは独立（別の task queue）。
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
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

#: 1 需要の実行。収集の上限時間に余裕を足す（service 自身が時間を守る）
RUN_START_TO_CLOSE = timedelta(seconds=ACQUISITION_RUN_TIMEOUT_SECONDS + 60)
LIST_START_TO_CLOSE = timedelta(seconds=30)

RUN_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=10),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=BGM_ACQUISITION_MAX_ATTEMPTS,
    non_retryable_error_types=["ValueError", "ValidationError"],
)
LIST_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
    maximum_attempts=BGM_ACQUISITION_MAX_ATTEMPTS,
)


@workflow.defn(name=BGM_ACQUISITION_WORKFLOW_NAME)
class BgmAcquisitionWorkflow:
    @workflow.run
    async def run(self, request: BgmAcquisitionInput) -> BgmAcquisitionOutput:
        limit = max(0, min(request.max_demands, BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW))
        if limit == 0:
            return BgmAcquisitionOutput()
        demands = await workflow.execute_activity(
            BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY,
            ListDemandsRequest(limit=limit),
            result_type=list[DemandRef],
            start_to_close_timeout=LIST_START_TO_CLOSE,
            retry_policy=LIST_RETRY_POLICY,
        )
        results: list[DemandRunResult] = []
        for demand in demands[:limit]:
            try:
                result = await workflow.execute_activity(
                    BGM_ACQUISITION_RUN_DEMAND_ACTIVITY,
                    demand,
                    result_type=DemandRunResult,
                    start_to_close_timeout=RUN_START_TO_CLOSE,
                    retry_policy=RUN_RETRY_POLICY,
                )
            except ActivityError:
                result = DemandRunResult(demand_sha256=demand.demand_sha256, status="failed")
            results.append(result)
        return BgmAcquisitionOutput(results=tuple(results))


__all__ = ["BgmAcquisitionWorkflow"]
