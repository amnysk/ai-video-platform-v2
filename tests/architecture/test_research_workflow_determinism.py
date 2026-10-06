"""ResearchWorkflow は I/O・壁時計・乱数・DB を使わない（決定性。ADR-0037 §8.5 / INV-4）。

Workflow が壁時計・乱数・DB・ファイル・ネットワークに触ると、replay で履歴と食い違い、走行中の依頼が
壊れる。外部との通信は Activity（**名前**で呼ぶ。INV-3）だけ。依頼の期限は executor が DB の
``started_at`` から測るので、Workflow は時刻を読まない。

旧 ``claude/research`` の同名テストの意味を移植した（Workflow は 1 本の Activity と失敗の記録だけに
なったので、``workflow.time`` も使わない）。理由は docs/testing/research-worker-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / "workers" / "research" / "workflows.py"
BANNED_ROOTS = {
    "sqlalchemy",
    "httpx",
    "requests",
    "aiohttp",
    "socket",
    "random",
    "secrets",
    "uuid",
    "time",
    "os",
    "pathlib",
    "subprocess",
    "minio",
    "infrastructure",
    "apps",
}
WALL_CLOCK_ATTRS = {"now", "utcnow", "today", "time", "time_ns", "monotonic", "perf_counter"}


def _tree() -> ast.Module:
    return ast.parse(WORKFLOWS.read_text(encoding="utf-8"), filename=str(WORKFLOWS))


def _imports() -> list[str]:
    names: list[str] = []
    for node in ast.walk(_tree()):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_the_workflow_module_exists() -> None:
    assert WORKFLOWS.is_file()
    assert "ResearchWorkflow" in WORKFLOWS.read_text(encoding="utf-8")


def test_the_workflow_imports_no_io_libraries_and_no_infrastructure() -> None:
    bad = [n for n in _imports() if n.split(".")[0] in BANNED_ROOTS]
    assert not bad, bad


def test_the_workflow_never_reads_a_clock_or_randomness() -> None:
    calls = [
        ast.unparse(n.func)
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and (n.func.attr in WALL_CLOCK_ATTRS or n.func.attr in {"random", "uuid4", "uuid1"})
    ]
    assert not calls, calls


def test_the_workflow_calls_activities_by_name_not_by_implementation() -> None:
    """INV-3: activities.py（実装）を import しない。名前と型は contracts から。"""
    assert not [n for n in _imports() if n.startswith("workers.")]
    source = WORKFLOWS.read_text(encoding="utf-8")
    assert "execute_activity_method" not in source
    assert "RESEARCH_EXECUTE_ACTIVITY" in source and "RESEARCH_RECORD_FAILURE_ACTIVITY" in source


def test_every_activity_call_is_bounded() -> None:
    """全ての Activity 呼び出しに timeout と retry policy（上限つき）を渡す。

    無限 retry をしない。
    """
    calls = [
        n
        for n in ast.walk(_tree())
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "execute_activity"
    ]
    assert len(calls) == 2
    for call in calls:
        keywords = {k.arg for k in call.keywords}
        assert {"start_to_close_timeout", "retry_policy", "result_type"} <= keywords
