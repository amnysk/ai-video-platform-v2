"""agent-worker の compose 構成（ADR-0043 §6、INV-55）。

各テストの理由: docs/testing/jpmas2-p1b-rationale.md
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts.agentic_pipeline import AGENTS_TASK_QUEUE

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def svc() -> dict[str, Any]:
    data = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    return data["services"]["agent-worker"]


def test_agent_worker_runs_only_under_the_agentic_profile(svc) -> None:
    """既定の ``make up``（core）で起動しない。core に入れる変更は別の承認が要る。"""
    assert svc["profiles"] == ["agentic"]


def test_agent_worker_command_and_health_queue(svc) -> None:
    assert svc["command"] == [
        "python",
        "-m",
        "infrastructure.runtime.worker_entry",
        "workers.agents.run_worker",
    ]
    test = svc["healthcheck"]["test"]
    assert test[test.index("--queue") + 1] == AGENTS_TASK_QUEUE


def test_agentic_flags_default_to_off_in_compose(svc) -> None:
    env = svc["environment"]
    assert env["JPMAS2_AGENTIC_WORKFLOWS"] == "${JPMAS2_AGENTIC_WORKFLOWS:-false}"
    assert env["JPMAS2_ENABLED_AGENTS"] == "${JPMAS2_ENABLED_AGENTS:-}"
    assert env["JPMAS2_AGENT_LLM"] == "${JPMAS2_AGENT_LLM:-codex}"
    # 換算率に既定を置かない: 未設定なら budget_unverifiable で止まる
    assert env["BUDGET_USD_JPY_RATE"] == "${BUDGET_USD_JPY_RATE:-}"


def test_agent_worker_holds_no_paid_provider_or_youtube_secret(svc) -> None:
    keys = set(svc["environment"])
    assert not {k for k in keys if k.startswith(("FAL_", "YOUTUBE_"))}
    for volume in svc.get("volumes") or []:
        assert "youtube" not in str(volume).lower()


def test_agent_worker_is_not_in_the_deploy_script() -> None:
    text = (ROOT / "scripts/deploy-workers.sh").read_text(encoding="utf-8")
    assert not re.search(r"\bagent-worker\b", text)
