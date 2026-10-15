"""INV-55: 日次経路は agentic のモジュールを参照しない（ADR-0043 §5）。

対象は **直接の import** を AST で見る（動的 import・推移的な依存は見ない）。
日次経路 = ``EpisodePipelineWorkflow``（``workers/pipeline/workflows.py``）・
Topic Planner / Script の worker（``workers/planning/**``）・
Schedule 定義（``infrastructure/temporal/schedules.py``）。

``workers/planning`` には P1 が「TopicPlan が無く現行の ``creative_brief`` Artifact がある
Episode」の分岐を足す（ADR-0043 §6）。そこで読む語彙は ``contracts.agentic.CreativeBrief`` と
``contracts.artifact_registry`` だけなので、これだけを許す（他は agentic の語彙を持ち込めない）。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

AGENTIC_MODULES = (
    "contracts.agents",
    "contracts.workflow_templates",
    "contracts.agentic",
    "domain.agents",
    "domain.workflow_templates",
    "application",
)

#: ``workers/planning`` にだけ許す import（module -> 取り込める名前。None = module ごと可）
PLANNING_ALLOWED: dict[str, frozenset[str] | None] = {
    "contracts.agentic": frozenset({"CreativeBrief"}),
    "contracts.artifact_registry": None,
}

DAILY_FILES = (
    "workers/pipeline/workflows.py",
    "infrastructure/temporal/schedules.py",
)


def _is_agentic(module: str) -> bool:
    return any(module == m or module.startswith(m + ".") for m in AGENTIC_MODULES)


def _violations_in(source: str, label: str, allowed: dict[str, frozenset[str] | None]) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_agentic(alias.name) and alias.name not in allowed:
                    found.append(f"{label}: imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            module = node.module
            # ``from contracts import agents`` のような形も agentic として扱う
            targets = [module] + [f"{module}.{a.name}" for a in node.names]
            if not any(_is_agentic(t) for t in targets):
                continue
            if module in allowed:
                names = allowed[module]
                if names is None or {a.name for a in node.names} <= names:
                    continue
            found.append(f"{label}: imports from {module}")
    return found


def _violations(path: pathlib.Path, allowed: dict[str, frozenset[str] | None]) -> list[str]:
    return _violations_in(path.read_text(encoding="utf-8"), str(path.relative_to(REPO)), allowed)


def test_daily_path_modules_import_nothing_agentic() -> None:
    violations: list[str] = []
    for rel in DAILY_FILES:
        violations += _violations(REPO / rel, {})
    assert not violations, "\n".join(violations) + "\n(INV-55)"


def test_the_planning_worker_may_only_read_the_creative_brief() -> None:
    files = sorted((REPO / "workers" / "planning").rglob("*.py"))
    assert files, "workers/planning が見つからない"
    violations: list[str] = []
    for path in files:
        violations += _violations(path, PLANNING_ALLOWED)
    assert not violations, "\n".join(violations) + "\n(INV-55)"


def test_the_checker_catches_each_import_form_and_checks_allowed_names() -> None:
    """検査自体の検査: import の書き方ごとに拾い、許可表は名前まで見る。"""
    source = (
        "import contracts.agents\n"
        "from contracts import agents\n"
        "from domain.agents.tool_gateway import ToolGateway\n"
        "from application.errors import OperationError\n"
        "from contracts.agentic import StrategyBrief\n"
    )
    assert len(_violations_in(source, "x", {})) == 5
    assert len(_violations_in(source, "x", PLANNING_ALLOWED)) == 5  # StrategyBrief は許可外
    allowed_only = (
        "from contracts.agentic import CreativeBrief\nfrom contracts.artifact_registry import x\n"
    )
    assert _violations_in(allowed_only, "x", PLANNING_ALLOWED) == []
    assert len(_violations_in(allowed_only, "x", {})) == 1  # artifact_registry は語彙の外
