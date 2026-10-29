"""INV-105: PASS/WARN/REVIEW/BLOCK の語彙と厳しさの順序は ``contracts/verdict.py`` だけが持つ。

``contracts`` / ``domain`` / ``workers`` / ``apps`` / ``infrastructure`` の他のモジュールが
同じ判定語彙を literal で再定義してはならない（AGENTS.md §8）。検査器は違反 fixture を
実際に捕まえることも別テストで示す。理由は docs/testing/verdict-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
OWNER = "contracts/verdict.py"
SCAN_DIRS = ("contracts", "domain", "workers", "apps", "infrastructure")
VERDICT_VALUES = frozenset({"pass", "warn", "review", "block"})


def verdict_redefinitions(source: str) -> list[str]:
    """判定語彙 4 値のうち 3 値以上を持つ class・集合・列・辞書 literal の違反を返す。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef):
            values = {
                n.value.value
                for n in node.body
                if isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            }
            if len(values & VERDICT_VALUES) >= 3:
                found.append(f"class {node.name} redefines the verdict set")
        elif isinstance(node, ast.Set | ast.List | ast.Tuple):
            values = {
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
            if len(values & VERDICT_VALUES) >= 3:
                found.append(f"literal at line {node.lineno} redefines the verdict set")
        elif isinstance(node, ast.Dict):
            keys = {
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            if len(keys & VERDICT_VALUES) >= 3:
                found.append(f"dict at line {node.lineno} redefines the verdict set")
    return found


def _files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for d in SCAN_DIRS:
        out.extend(sorted((REPO / d).rglob("*.py")))
    return out


def test_only_contracts_verdict_defines_the_verdict_set() -> None:
    violations = [
        f"{p.relative_to(REPO)}: {v}"
        for p in _files()
        if p.relative_to(REPO).as_posix() != OWNER
        for v in verdict_redefinitions(p.read_text(encoding="utf-8"))
    ]
    assert not violations, "\n".join(violations)


def test_the_owner_exists_and_is_the_definition() -> None:
    assert verdict_redefinitions((REPO / OWNER).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        'from enum import StrEnum\nclass V(StrEnum):\n    A="pass"\n    B="warn"\n    C="block"\n',
        'ORDER = ("pass", "warn", "review", "block")\n',
        'S = {"pass", "review", "block"}\n',
        'RANK = {"pass": 0, "warn": 1, "block": 3}\n',
    ],
)
def test_the_detector_catches_a_redefinition(source: str) -> None:
    assert verdict_redefinitions(source)


def test_the_detector_ignores_unrelated_literals() -> None:
    assert not verdict_redefinitions('X = ("pass", "fail")\nY = {"warn": 1}\n')
