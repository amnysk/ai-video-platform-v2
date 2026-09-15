"""Pipeline worker の登録（ADR-0023）。"""

from __future__ import annotations

from contracts.pipeline import PIPELINE_ACTIVITY_NAMES, PIPELINE_TASK_QUEUE
from workers.pipeline.activities import PipelineActivities


def test_all_contract_activity_names_are_registered(session_factory) -> None:
    acts = PipelineActivities(
        session_factory=session_factory, paused_env=False, uploads_paused_env=False
    )
    names = {fn.__temporal_activity_definition.name for fn in acts.activities()}  # type: ignore[attr-defined]
    assert names == set(PIPELINE_ACTIVITY_NAMES)
    assert PIPELINE_TASK_QUEUE == "pipeline"


def test_run_worker_module_imports() -> None:
    from workers.pipeline import run_worker

    assert callable(run_worker.build_worker)
