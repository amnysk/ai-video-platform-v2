"""opensearch-guard の導入物（unit・check-pipeline.sh）の契約（ADR-0059 / INV-79 / INV-80）。

理由は docs/testing/opensearch-guard-rationale.md（deploy の節）。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HM = ROOT / "deploy" / "host-monitor"


def _exec(name: str) -> str:
    m = re.search(r"^ExecStart=(.+)$", (HM / name).read_text(), re.M)
    assert m, name
    return m.group(1)


def test_check_pipeline_no_longer_stops_any_container() -> None:
    """1 サンプルで止める旧挙動（--enforce-memory）の再発を落とす。"""
    code = [
        ln
        for ln in (ROOT / "deploy/logging/scripts/check-pipeline.sh").read_text().splitlines()
        if not ln.lstrip().startswith("#")
    ]
    assert not [ln for ln in code if "docker stop" in ln or "stop_service" in ln], code


def test_installed_logging_check_unit_does_not_enable_the_retired_flag() -> None:
    cmd = _exec_of(ROOT / "deploy/logging/systemd/avp-logging-check.service")
    assert "--enforce-memory" not in cmd


def _exec_of(path: Path) -> str:
    m = re.search(r"^ExecStart=(.+)$", path.read_text(), re.M)
    assert m
    return m.group(1)


def test_guard_unit_is_stop_only_by_default_auto_restart_is_a_separate_decision() -> None:
    cmd = _exec("avp-opensearch-guard.service")
    assert "opensearch-guard.py" in cmd and "--auto-restart" not in cmd
    assert "compose" not in (HM / "avp-opensearch-guard.service").read_text()


def test_guard_timer_samples_every_30_seconds() -> None:
    assert "OnUnitActiveSec=30s" in (HM / "avp-opensearch-guard.timer").read_text()


def test_watch_is_a_separate_unit_and_timer_from_the_monitors_it_watches() -> None:
    cmd = _exec("avp-monitor-watch.service")
    assert "monitor-watch.py" in cmd
    timer = (HM / "avp-monitor-watch.timer").read_text()
    assert "OnUnitActiveSec=" in timer
    assert "Unit=" not in timer  # 既定どおり同名の service。guard の unit に相乗りしない


def test_units_do_not_hide_in_a_checkout_that_can_be_deleted() -> None:
    """ExecStart は ~/.local/lib/avp-host/ のコピー（worktree を消すと止まった前例を繰り返さない）。"""  # noqa: E501
    for name in (
        "avp-opensearch-guard.service",
        "avp-monitor-watch.service",
        "avp-host-monitor.service",
    ):
        assert ".local/lib/avp-host/" in _exec(name), name
        assert "worktrees" not in _exec(name) and "projects/" not in _exec(name)
