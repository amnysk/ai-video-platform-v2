"""レイヤ依存の検査（INV-3 / INV-6）。

このテストは Phase 0 の時点で既に有効である。パッケージが空のうちは自明に通るが、
最初の違反importが入った瞬間に落ちる。「実装してから検査を足す」を避けるための配置。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# 各レイヤがimportしてよいトップレベルパッケージ（docs/architecture/overview.md の表）
ALLOWED: dict[str, set[str]] = {
    "contracts": set(),
    "domain": {"contracts"},
    "infrastructure": {"contracts"},
    "workers": {"domain", "contracts", "infrastructure"},
    "apps": {"domain", "contracts", "infrastructure"},
}
LAYERS = set(ALLOWED)


def _python_files(layer: str) -> list[pathlib.Path]:
    return sorted((REPO / layer).rglob("*.py"))


def _imported_roots(path: pathlib.Path) -> set[tuple[str, str]]:
    """(トップレベルパッケージ, 完全なモジュール名) の集合を返す。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add((alias.name.split(".")[0], alias.name))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add((node.module.split(".")[0], node.module))
    return found


def test_layer_dependencies_are_one_directional() -> None:
    """INV-6: 依存は overview.md の表の向きだけ。"""
    violations: list[str] = []
    for layer, allowed in ALLOWED.items():
        for path in _python_files(layer):
            for root, module in _imported_roots(path):
                if root in LAYERS and root != layer and root not in allowed:
                    rel = path.relative_to(REPO)
                    violations.append(f"{rel}: {layer} must not import {module} (INV-6)")
    assert not violations, "\n".join(violations)


def test_workers_do_not_import_other_workers() -> None:
    """INV-3: worker同士は疎結合。共有したいものは domain/ か infrastructure/ へ降ろす。"""
    violations: list[str] = []
    for path in _python_files("workers"):
        rel = path.relative_to(REPO)
        own = rel.parts[1] if len(rel.parts) > 2 else None
        if own is None:
            continue
        for _root, module in _imported_roots(path):
            parts = module.split(".")
            if parts[0] == "workers" and len(parts) > 1 and parts[1] != own:
                violations.append(f"{rel}: imports {module} (INV-3)")
    assert not violations, "\n".join(violations)


def test_domain_has_no_io_dependencies() -> None:
    """INV-6: domain/ は純粋。DB・HTTP・Temporal・ファイルI/Oに触れない。"""
    banned = {
        "sqlalchemy",
        "asyncpg",
        "psycopg",
        "psycopg2",
        "temporalio",
        "fastapi",
        "starlette",
        "httpx",
        "requests",
        "boto3",
        "minio",
        "aiofiles",
    }
    violations: list[str] = []
    for path in _python_files("domain"):
        for root, module in _imported_roots(path):
            if root in banned:
                violations.append(f"{path.relative_to(REPO)}: imports {module} (INV-6)")
    assert not violations, "\n".join(violations)
