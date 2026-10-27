"""``FAL_MONTHLY_BUDGET_USD`` の読み方と、有料の入口がすべて予算ゲートに結線されている
（ADR-0061 / INV-84）。未設定を無制限と読まない・読めない値で worker を落とさない。
"""

from __future__ import annotations

import pathlib

import pytest

from contracts.provider_health import BudgetStatus
from infrastructure.config import Settings

REPO = pathlib.Path(__file__).resolve().parents[2]


def _settings(monkeypatch: pytest.MonkeyPatch, value: str | None) -> Settings:
    monkeypatch.delenv("FAL_MONTHLY_BUDGET_USD", raising=False)
    if value is not None:
        monkeypatch.setenv("FAL_MONTHLY_BUDGET_USD", value)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("raw", "status", "allowed"),
    [
        (None, BudgetStatus.UNSET, False),  # 未設定 ≠ 無制限
        ("", BudgetStatus.UNSET, False),  # compose の ${VAR:-} が空文字を渡す
        ("   ", BudgetStatus.UNSET, False),
        ("0", BudgetStatus.DISABLED, False),  # 明示的な 0
        ("0.00", BudgetStatus.DISABLED, False),
        ("-5", BudgetStatus.UNSET, False),
        ("abc", BudgetStatus.UNSET, False),  # 読めない値で起動を落とさない
        ("nan", BudgetStatus.UNSET, False),
        ("inf", BudgetStatus.UNSET, False),
        ("30", BudgetStatus.ENABLED, True),
        ("12.5", BudgetStatus.ENABLED, True),
    ],
)
def test_budget_setting_is_read_fail_closed(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, status: BudgetStatus, allowed: bool
) -> None:
    decision = _settings(monkeypatch, raw).fal_budget
    assert decision.status is status
    assert decision.submit_allowed is allowed


@pytest.mark.parametrize(
    "path", ["workers/production_image/run_worker.py", "workers/production_video/run_worker.py"]
)
def test_every_paid_worker_passes_the_budget_to_its_runner(path: str) -> None:
    """結線を忘れると PaidJobRunner は予算を見ない（None = 結線なし）。本番の入口で必ず渡す。"""
    src = (REPO / path).read_text()
    assert "PaidJobRunner(" in src
    assert "budget=settings.fal_budget" in src


def test_the_pipeline_worker_passes_the_budget_to_the_gates() -> None:
    src = (REPO / "workers/pipeline/run_worker.py").read_text()
    assert "build_gate_ports(session_factory, budget=settings.fal_budget)" in src


def test_every_runner_construction_in_production_code_is_in_a_wired_worker() -> None:
    """``PaidJobRunner(`` を組む production コードは上の 2 worker だけ。入口が増えたら落ちる。"""
    allowed = {
        "workers/production_image/run_worker.py",
        "workers/production_video/run_worker.py",
        "infrastructure/production/paid_job.py",
    }
    found = set()
    for root in ("apps", "workers", "infrastructure", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            if "PaidJobRunner(" in path.read_text():
                found.add(str(path.relative_to(REPO)))
    assert found - allowed == set(), found - allowed
