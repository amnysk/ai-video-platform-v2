"""INV-48: アシスタント adapter は介入 HTTP API と contracts だけを使う（ADR-0042 §1）。

``apps/assistant/**`` が import してよいのは stdlib・httpx・``contracts.*``・自分自身だけ。
永続化・Temporal・worker・domain・API の内部に触れると、権限・期待版・scope の記録（INV-47）を
迂回する経路になる。理由は docs/testing/assistant-rationale.md §3。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ASSISTANT = REPO / "apps" / "assistant"

ALLOWED_THIRD_PARTY = {"httpx"}
ALLOWED_FIRST_PARTY = {"contracts", "apps.assistant"}
FORBIDDEN = (
    "infrastructure",
    "workers",
    "domain",
    "temporalio",
    "sqlalchemy",
    "psycopg",
    "asyncpg",
    "aiosqlite",
    "apps.api",
    "apps.web",
    "fastapi",
    "starlette",
    "boto3",
    "minio",
)


def _imports(tree: ast.AST) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                out.append("apps.assistant")  # 相対 import は同じ package 内
            elif node.module:
                out.append(node.module)
    return out


def _violations(source: str, rel: str) -> list[str]:
    bad: list[str] = []
    for name in _imports(ast.parse(source)):
        top = name.split(".")[0]
        if top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY:
            continue
        if any(name == a or name.startswith(a + ".") for a in ALLOWED_FIRST_PARTY):
            continue
        bad.append(f"{rel}: imports {name}")
    return bad


def test_the_assistant_package_imports_only_http_and_contracts() -> None:
    files = sorted(ASSISTANT.rglob("*.py"))
    assert {f.name for f in files} >= {"__init__.py", "client.py", "tools.py"}
    bad: list[str] = []
    for path in files:
        bad += _violations(path.read_text(encoding="utf-8"), path.relative_to(REPO).as_posix())
    assert not bad, "\n".join(bad)


def test_no_forbidden_module_is_named_even_in_a_lazy_import() -> None:
    """関数内・TYPE_CHECKING の import も ``ast.walk`` で拾う。"""
    hits = []
    for path in ASSISTANT.rglob("*.py"):
        for name in _imports(ast.parse(path.read_text(encoding="utf-8"))):
            if any(name == f or name.startswith(f + ".") for f in FORBIDDEN):
                hits.append(f"{path.name}: {name}")
    assert not hits, hits


def test_the_cli_entry_point_only_wraps_the_adapter() -> None:
    cli = REPO / "scripts" / "assistant.py"
    bad = _violations(cli.read_text(encoding="utf-8"), "scripts/assistant.py")
    assert not bad, bad


def test_the_check_itself_rejects_internal_imports() -> None:
    source = (
        "import json\n"
        "import httpx\n"
        "from contracts.intervention import Scope\n"
        "from infrastructure.intervention.store import InterventionStore\n"
        "def f():\n"
        "    import sqlalchemy\n"
        "    from apps.api.routers import intervention\n"
    )
    bad = _violations(source, "example.py")
    assert [b.split(" ", 2)[2] for b in bad] == [
        "infrastructure.intervention.store",
        "sqlalchemy",
        "apps.api.routers",
    ]
