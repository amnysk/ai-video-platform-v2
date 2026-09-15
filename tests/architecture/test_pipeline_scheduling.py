"""INV-2: スケジューラは Temporal の Schedule / workflow start だけを行う（ADR-0023）。

- Temporal Schedule を作る API（``create_schedule`` / ``ScheduleActionStartWorkflow``）を
  使ってよいのは
  ``infrastructure/temporal/schedules.py`` だけ（CLI はそれを呼ぶ）
- プロセス内の cron ライブラリを使わない
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
LAYERS = ("apps", "workers", "domain", "infrastructure", "contracts", "scripts")
SCHEDULE_OWNER = pathlib.Path("infrastructure/temporal/schedules.py")
SCHEDULE_NAMES = {"create_schedule", "ScheduleActionStartWorkflow", "get_schedule_handle"}
CRON_LIBRARIES = {"apscheduler", "schedule", "crontab", "croniter", "aiocron", "rocketry"}


def _files() -> list[pathlib.Path]:
    return [p for layer in LAYERS for p in sorted((REPO / layer).rglob("*.py"))]


def test_only_the_schedule_module_creates_temporal_schedules() -> None:
    violations: list[str] = []
    for path in _files():
        rel = path.relative_to(REPO)
        if rel == SCHEDULE_OWNER or rel.parts[0] == "scripts":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else None
            )
            if name in SCHEDULE_NAMES:
                violations.append(f"{rel}:{getattr(node, 'lineno', 0)}: {name} (INV-2)")
    assert not violations, "\n".join(violations)


def test_no_in_process_cron_libraries() -> None:
    violations: list[str] = []
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules = [node.module]
            for module in modules:
                if module.split(".")[0] in CRON_LIBRARIES:
                    violations.append(f"{path.relative_to(REPO)}: imports {module} (INV-2)")
    assert not violations, "\n".join(violations)
