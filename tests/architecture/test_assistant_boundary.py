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


def _dynamic_target(node: ast.Call) -> str | None:
    """``importlib.import_module`` / ``__import__`` の呼び出しなら、その引数（不明なら ``?``）。"""
    name = ast.unparse(node.func)
    if name.split(".")[-1] not in {"import_module", "__import__"}:
        return None
    first = node.args[0] if node.args else None
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    return "?"  # リテラルでない: 何を読むか検査できないので違反として扱う


def _imports(tree: ast.AST) -> list[str]:
    """import される名前。``from a import b`` は ``a.b`` に展開する。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level == 1:
                out.append("apps.assistant")  # 同じ package 内
            elif node.level > 1:
                out.append("<relative import above the package>")
            else:
                base = node.module or ""
                out += [f"{base}.{a.name}" if a.name != "*" else base for a in node.names]
        elif isinstance(node, ast.Call):
            target = _dynamic_target(node)
            if target is not None:
                out.append(target)
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


def _scan(directory: Path) -> list[str]:
    bad: list[str] = []
    for path in sorted(directory.rglob("*.py")):
        bad += _violations(path.read_text(encoding="utf-8"), path.name)
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
        "infrastructure.intervention.store.InterventionStore",
        "sqlalchemy",
        "apps.api.routers.intervention",
    ]


def test_from_apps_import_assistant_is_not_a_false_positive() -> None:
    assert (
        _violations("from apps import assistant\nfrom contracts import intervention\n", "x") == []
    )
    assert _violations("from apps import api\n", "x") != []
    assert _violations("from infrastructure import db\n", "x") != []


def test_each_detection_fires_on_a_fake_package(tmp_path: Path) -> None:
    """検査が空振りしないこと。実ファイルでなく tmp の偽 package で、検出ごとに1つ作る。"""
    cases = {
        "plain.py": "import sqlalchemy\n",
        "from_form.py": "from infrastructure.db import models\n",
        "from_package.py": "from apps import api\n",
        "lazy.py": "def f():\n    from workers import x\n",
        "relative_up.py": "from .. import api\n",
        "relative_up_named.py": "from ..api import routers\n",
        "import_module.py": "import importlib\nimportlib.import_module('temporalio.client')\n",
        "dunder.py": "__import__('psycopg')\n",
        "dynamic.py": "import importlib\nname = 'x'\nimportlib.import_module(name)\n",
        "alias_call.py": "from importlib import import_module\nimport_module('domain.x')\n",
    }
    for name, source in cases.items():
        package = tmp_path / name.removesuffix(".py")
        package.mkdir()
        (package / name).write_text(source, encoding="utf-8")
        assert _scan(package), f"{name} was not detected"
    ok = tmp_path / "ok"
    ok.mkdir()
    (ok / "ok.py").write_text(
        "import json\nimport httpx\nfrom . import client\nfrom .tools import x\n"
        "from contracts.intervention import Scope\nimport importlib\n"
        "importlib.import_module('json')\nfrom apps import assistant\n",
        encoding="utf-8",
    )
    assert _scan(ok) == []
