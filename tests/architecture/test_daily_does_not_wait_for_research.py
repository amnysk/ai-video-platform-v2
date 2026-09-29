"""日次の企画・Episode の工程は Research を起動しない・待たない（INV-37、ADR-0037 §7 / §8.5）。

Research は補助機能。日次（Daily / Episode pipeline）と企画（Topic Planner）・台本の workflow と
その worker は、Research の workflow・queue・worker・実行器を名指さない。名指しが無ければ、
Research が止まっても（Provider ``none`` で ``blocked``、worker が落ちている、など）日次は待たず、
失敗もしない。

企画・台本からの opt-in 接続（確定済みの結果を DB から読むだけ）は B6 で入る。その段で読む経路を
足すときも、ここにある「起動しない・待たない」は変えない（``infrastructure.research`` の Gateway・
実行器と Research の workflow を名指さない）。

旧 ``claude/research`` の ``test_daily_does_not_wait_for_research.py`` の意味を移植した
（Trend の定期更新 Schedule と ``require_fresh`` は移植していないので、その検査は無い。
ADR-0037 §9）。理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: 日次・企画・台本の workflow とその worker（Research を起動・待機してはならない側）
DAILY_SIDE_FILES = (
    "workers/pipeline/workflows.py",
    "workers/pipeline/activities.py",
    "workers/pipeline/run_worker.py",
    "workers/pipeline/watchdog.py",
    "workers/planning/workflows.py",
    "workers/planning/topic_workflows.py",
    "workers/planning/topic_activities.py",
    "workers/planning/run_worker.py",
)

#: これらの module を import しない（Research の worker・起動・Gateway・実行器）
FORBIDDEN_MODULES = (
    "workers.research",
    "infrastructure.research",
    "infrastructure.temporal.research_starter",
)
#: これらの名前を参照しない（Research の workflow・queue・起動を名指さない）
FORBIDDEN_NAMES = frozenset(
    {
        "RESEARCH_WORKFLOW",
        "RESEARCH_WORKFLOW_NAME",
        "RESEARCH_TASK_QUEUE",
        "RESEARCH_EXECUTE_ACTIVITY",
        "RESEARCH_RECORD_FAILURE_ACTIVITY",
        "ResearchWorkflowInput",
        "research_workflow_id",
        "start_research_workflow",
        "ResearchWorkflow",
        "ResearchGateway",
        "ResearchExecutor",
    }
)
#: workflow 名・queue 名・Activity 名を文字列で名指す抜け道も塞ぐ
FORBIDDEN_STRINGS = frozenset(
    {"ResearchWorkflow", "research", "research_execute", "research_record_failure"}
)


def _tree(relative: str) -> ast.AST:
    path = REPO / relative
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_the_daily_side_files_exist() -> None:
    """検査対象が消えて自明に通る、を防ぐ。"""
    for relative in DAILY_SIDE_FILES:
        assert (REPO / relative).is_file(), relative


@pytest.mark.parametrize("relative", DAILY_SIDE_FILES)
def test_daily_side_code_does_not_import_research_execution(relative: str) -> None:
    offenders: list[str] = []
    for node in ast.walk(_tree(relative)):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules = [node.module]
        offenders += [
            m for m in modules if any(m == f or m.startswith(f + ".") for f in FORBIDDEN_MODULES)
        ]
    assert not offenders, f"{relative} imports {offenders} (must not start/wait research)"


@pytest.mark.parametrize("relative", DAILY_SIDE_FILES)
def test_daily_side_code_does_not_name_research_workflows_or_queues(relative: str) -> None:
    offenders: set[str] = set()
    for node in ast.walk(_tree(relative)):
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            offenders.add(node.id)
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            offenders.add(node.attr)
        elif isinstance(node, ast.alias) and node.name in FORBIDDEN_NAMES:
            offenders.add(node.name)
        elif isinstance(node, ast.Constant) and node.value in FORBIDDEN_STRINGS:
            offenders.add(str(node.value))
    assert not offenders, (
        f"{relative} references {sorted(offenders)} (must not start/wait research)"
    )


def test_no_other_worker_registers_the_research_workflow() -> None:
    """``ResearchWorkflow`` を登録する worker は research-worker だけ（他の queue で走らない）。"""
    offenders = []
    for path in sorted(REPO.glob("workers/**/run_worker.py")):
        rel = path.relative_to(REPO)
        if rel.parts[1] == "research":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
        if names & FORBIDDEN_NAMES:
            offenders.append(str(rel))
    assert not offenders, offenders
