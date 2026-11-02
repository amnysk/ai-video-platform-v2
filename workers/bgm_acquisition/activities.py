"""BGM 収集の Activity 群（``AcquisitionService`` の薄いラッパ）。順序と retry は Workflow。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from temporalio import activity

from contracts.bgm_acquisition_activities import (
    BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY,
    BGM_ACQUISITION_RUN_DEMAND_ACTIVITY,
    DemandRef,
    DemandRunResult,
    ListDemandsRequest,
)
from contracts.music_sources import ACQUISITION_RUN_TIMEOUT_SECONDS, AcquisitionOutcome
from domain.music.ports import BgmCatalogRepository
from infrastructure.music.acquisition import AcquisitionService


def summarize(demand_sha256: str, summary: Any) -> DemandRunResult:
    """``AcquisitionRunSummary``（または ``None`` = 間隔内）を件数だけの結果へ。"""
    if summary is None:
        return DemandRunResult(demand_sha256=demand_sha256, status="rate_limited")
    counts = {o: 0 for o in AcquisitionOutcome}
    for candidate in summary.candidates:
        counts[candidate.outcome] += 1
    return DemandRunResult(
        demand_sha256=demand_sha256,
        status="ran",
        searches=summary.searches,
        registered_approved=counts[AcquisitionOutcome.REGISTERED_APPROVED],
        registered_quarantined=counts[AcquisitionOutcome.REGISTERED_QUARANTINED],
        duplicates=counts[AcquisitionOutcome.DUPLICATE],
        rejected=counts[AcquisitionOutcome.LICENSE_REJECTED],
        failed=(
            counts[AcquisitionOutcome.DOWNLOAD_FAILED]
            + counts[AcquisitionOutcome.VALIDATION_FAILED]
            + counts[AcquisitionOutcome.SOURCE_UNAVAILABLE]
        ),
    )


class BgmAcquisitionActivities:
    def __init__(self, *, repo: BgmCatalogRepository, service: AcquisitionService) -> None:
        self._repo = repo
        self._service = service

    @activity.defn(name=BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY)
    async def list_demands(self, request: ListDemandsRequest) -> list[DemandRef]:
        pending = await self._repo.pending_demands(limit=max(0, request.limit))
        return [DemandRef(demand_sha256=sha, requirements=dict(req)) for sha, req in pending]

    @activity.defn(name=BGM_ACQUISITION_RUN_DEMAND_ACTIVITY)
    async def run_demand(self, demand: DemandRef) -> DemandRunResult:
        summary = await asyncio.wait_for(
            self._service.run(demand.demand_sha256, demand.requirements),
            timeout=ACQUISITION_RUN_TIMEOUT_SECONDS,
        )
        return summarize(demand.demand_sha256, summary)

    def all_activities(self) -> Sequence[Callable[..., object]]:
        return [self.list_demands, self.run_demand]


__all__ = ["BgmAcquisitionActivities", "summarize"]
