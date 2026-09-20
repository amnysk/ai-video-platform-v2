"""Schedule を「解除」してよい場所を固定する（INV-25 / ADR-0027）。

自動で Schedule を unpause できるのはガード（印のある maintenance pause だけを解除する）だけ。
worker / API / その他が ``unpause`` を呼べると、運用者の emergency pause を外す経路が増える。
運用者が明示的に実行する ``scripts/ensure-daily-schedule.py --unpause`` は例外。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
LAYERS = ("apps", "workers", "domain", "infrastructure", "contracts", "scripts")
ALLOWED = {
    pathlib.Path("infrastructure/temporal/schedules.py"),  # TemporalScheduleControl の実装
    pathlib.Path("infrastructure/temporal/schedule_guard.py"),  # 印を確認して解除する唯一の場所
    pathlib.Path("scripts/ensure-daily-schedule.py"),  # 運用者が明示する --unpause
}


def _unpause_calls() -> list[str]:
    found: list[str] = []
    for layer in LAYERS:
        for path in sorted((REPO / layer).rglob("*.py")):
            rel = path.relative_to(REPO)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                is_unpause = isinstance(node, ast.Attribute) and node.attr == "unpause"
                if is_unpause and rel not in ALLOWED:
                    found.append(f"{rel}:{getattr(node, 'lineno', 0)}")
    return found


def test_only_the_guard_and_the_operator_script_unpause_schedules() -> None:
    assert not _unpause_calls(), (
        "Schedule の unpause は infrastructure/temporal/schedule_guard.py 経由だけ（INV-25）:\n"
        + "\n".join(_unpause_calls())
    )


def test_the_guard_checks_the_marker_before_it_unpauses() -> None:
    """``unpause`` を呼ぶ前に、pause が emergency でないと確認する分岐がある。"""
    source = (REPO / "infrastructure/temporal/schedule_guard.py").read_text(encoding="utf-8")
    assert source.count("control.unpause(") == 2
    assert source.count("PAUSED_UNEXPECTEDLY") >= 2  # begin / end の拒否
    assert "MAINTENANCE_EXPIRED" in source  # reconcile は期限切れだけ
