"""agent worker の起動条件（ADR-0043 §6、INV-55）。外部へ接続しない。

各テストの理由: docs/testing/jpmas2-p1b-rationale.md
"""

from __future__ import annotations

import pytest

from contracts.agentic_pipeline import AGENTS_TASK_QUEUE, RUN_AGENT_STAGE_ACTIVITY
from workers.agents import run_worker
from workers.agents.activities import AgentActivities


async def test_the_worker_exits_with_a_clear_message_when_the_flag_is_off(monkeypatch) -> None:
    monkeypatch.setenv("JPMAS2_AGENTIC_WORKFLOWS", "false")

    async def forbidden(*_a, **_k):
        raise AssertionError("must not connect to Temporal while the flag is off")

    monkeypatch.setattr(run_worker, "connect_with_retry", forbidden)
    with pytest.raises(SystemExit) as exc:
        await run_worker.main()
    assert "JPMAS2_AGENTIC_WORKFLOWS" in str(exc.value)


def test_the_activity_name_and_queue_come_from_the_contract() -> None:
    assert AGENTS_TASK_QUEUE == "agents"
    activities = AgentActivities.run_agent_stage
    assert getattr(activities, "__temporal_activity_definition").name == RUN_AGENT_STAGE_ACTIVITY
