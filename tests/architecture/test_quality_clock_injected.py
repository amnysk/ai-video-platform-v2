"""INV-107: 品質判定は実時計を読まない（時刻は引数で注入）。理由は verdict-rationale.md。"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PATHS = ("contracts/quality.py", "domain/quality")
CLOCK_ATTRS = frozenset({"now", "utcnow", "today", "time", "monotonic", "perf_counter"})


def clock_reads(source: str) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in CLOCK_ATTRS
        ):
            found.append(f"calls .{node.func.attr}() at line {node.lineno}")
        if isinstance(node, ast.ImportFrom) and node.module == "time":
            found.append("imports from time")
        if isinstance(node, ast.Import) and any(a.name == "time" for a in node.names):
            found.append("imports time")
    return found


def test_quality_modules_never_read_the_clock() -> None:
    files: list[pathlib.Path] = []
    for rel in PATHS:
        path = REPO / rel
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    assert any(f.name == "freshness.py" for f in files)
    violations = [
        f"{f.relative_to(REPO)}: {v}" for f in files for v in clock_reads(f.read_text("utf-8"))
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\nx = datetime.now()\n",
        "import datetime\nx = datetime.datetime.utcnow()\n",
        "import time\n",
        "from time import time\n",
    ],
)
def test_the_detector_catches_a_clock_read(source: str) -> None:
    assert clock_reads(source)
