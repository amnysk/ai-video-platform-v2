"""architecture テストが共有する import 検出（ADR-0084 §10。定義はここだけ）。

``import a.b`` / ``from a import b``（``a`` と ``a.b`` の両方。b がサブモジュールのことがある）/
相対 import（``package`` で絶対名に直す）/ ``import_module("x")`` ``__import__("x")`` の
定数引数を拾う。Phase 2 の ``test_geo_boundaries`` は ``from a import b`` の ``b`` と相対 import と
動的 import を見落としていた（Phase 2 報告 §4-8）。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def package_of(path: pathlib.Path) -> str:
    """ファイルの属するパッケージ（相対 import の解決用）。"""
    return ".".join(path.relative_to(REPO).parent.parts)


def modules_of_source(source: str, package: str = "") -> list[str]:
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                parts = parts[: len(parts) - (node.level - 1)] if node.level > 1 else parts
                base = ".".join([*parts, *([node.module] if node.module else [])])
            if base:
                found.append(base)
            found.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            first = node.args[0]
            if (
                name in {"import_module", "__import__"}
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
            ):
                found.append(first.value)
    return found


def modules_of_file(path: pathlib.Path) -> list[str]:
    return modules_of_source(path.read_text(encoding="utf-8"), package_of(path))


def py_files(path: pathlib.Path) -> list[pathlib.Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)
