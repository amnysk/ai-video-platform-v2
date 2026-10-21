"""日次異常レポートは読み取り専用。書き込み動詞・変更系の呼び出しを持たない。"""

from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
IO_FILES = [
    REPO / "infrastructure" / "diagnostics" / "anomaly_events.py",
    REPO / "scripts" / "daily-anomaly-report.py",
]
DOMAIN = REPO / "domain" / "anomaly_report.py"
FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE)\b|\.(commit|unpause|pause|trigger|terminate|signal"
    r"|start_workflow|execute_workflow|delete|toggle)\(|method=\"(POST|PUT|PATCH|DELETE)\"",
    re.IGNORECASE,
)


def test_io_modules_have_no_write_verbs() -> None:
    bad = []
    for path in IO_FILES:
        # docstring・コメントの説明文は除き、コード中の文字列だけを見る
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and FORBIDDEN.search(node.value)
                and len(node.value) < 400
            ):
                bad.append(f"{path.name}:{node.lineno}: {node.value[:60]}")
        code = re.sub(r'"""[\s\S]*?"""', "", path.read_text(encoding="utf-8"))
        for m in re.finditer(
            r"\.(commit|unpause|pause|trigger|terminate|signal|start_workflow|execute_workflow)\(",
            code,
        ):
            bad.append(f"{path.name}: {m.group(0)}")
    assert not bad, bad


def test_sql_in_the_loader_is_select_or_set_transaction_read_only() -> None:
    src = IO_FILES[0].read_text(encoding="utf-8")
    assert "SET TRANSACTION READ ONLY" in src
    for stmt in re.findall(r'text\(\s*"(\w+)', src):
        assert stmt in {"SELECT", "SET"}, stmt


def test_domain_module_is_pure() -> None:
    tree = ast.parse(DOMAIN.read_text(encoding="utf-8"))
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            imported.add((n.module or "").split(".")[0])
    assert not imported & {"sqlalchemy", "urllib", "temporalio", "socket", "os", "subprocess"}
