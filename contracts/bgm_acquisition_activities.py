"""BGM 収集 Workflow と Activity の名前・入出力（ADR-0087 §3）。

Workflow は Activity を**名前**で呼ぶ（INV-3: 実装を import しない）。履歴に載るのは需要の hash・
要件（小さな dict）・件数だけ。曲の中身・API 応答の原文は載せない。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

BGM_ACQUISITION_WORKFLOW_NAME = "BgmAcquisitionWorkflow"
BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY = "bgm_acquisition_list_demands"
BGM_ACQUISITION_RUN_DEMAND_ACTIVITY = "bgm_acquisition_run_demand"

#: 1 回の Workflow が処理する需要の上限（逐次）。各需要の上限は ``contracts.music_sources``
BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW = 3
#: Activity の試行回数の上限（初回 + 1 回）
BGM_ACQUISITION_MAX_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class BgmAcquisitionInput:
    max_demands: int = BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW


@dataclass(frozen=True, slots=True)
class DemandRef:
    demand_sha256: str
    requirements: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ListDemandsRequest:
    limit: int


@dataclass(frozen=True, slots=True)
class DemandRunResult:
    demand_sha256: str
    #: ``ran`` / ``rate_limited``（同じ需要を最小間隔内に実行済み）/ ``failed``（Workflow が付ける）
    status: str
    searches: int = 0
    registered_approved: int = 0
    registered_quarantined: int = 0
    duplicates: int = 0
    rejected: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class BgmAcquisitionOutput:
    results: tuple[DemandRunResult, ...] = ()


__all__ = [
    "BGM_ACQUISITION_LIST_DEMANDS_ACTIVITY",
    "BGM_ACQUISITION_MAX_ATTEMPTS",
    "BGM_ACQUISITION_MAX_DEMANDS_PER_WORKFLOW",
    "BGM_ACQUISITION_RUN_DEMAND_ACTIVITY",
    "BGM_ACQUISITION_WORKFLOW_NAME",
    "BgmAcquisitionInput",
    "BgmAcquisitionOutput",
    "DemandRef",
    "DemandRunResult",
    "ListDemandsRequest",
]
