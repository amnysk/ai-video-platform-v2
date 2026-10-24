"""0018 は expand-only で、release preflight の migration 走査を通る（ADR-0057 / INV-72）。

release gate は制約・索引・列の drop や型変更を止める（0017 が未解決の例）。0018 は
新しい表だけを足し、既存の CHECK（``provider_reservations`` を含む）を広げない。
"""

from __future__ import annotations

import ast
import pathlib

from domain import release_preflight as rp

REPO = pathlib.Path(__file__).resolve().parents[2]
MIGRATION = REPO / "infrastructure/db/migrations/versions/0018_provider_breaker_and_credit_holds.py"

#: upgrade() に出てよい alembic の操作（すべて追加）
ALLOWED_OPS = {"create_table", "create_index"}


def test_release_preflight_scanner_finds_nothing_in_0018() -> None:
    assert rp.scan_migration(MIGRATION.read_text(encoding="utf-8")) == []


def test_0018_upgrade_only_creates_tables_and_indexes() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    ops = {
        node.func.attr
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }
    assert ops <= ALLOWED_OPS, ops - ALLOWED_OPS


def test_0018_creates_only_its_own_tables() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    created = {
        arg.id
        for node in ast.walk(upgrade)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
        for arg in node.args[:1]
        if isinstance(arg, ast.Name)
    }
    assert created == {"BREAKERS", "HOLDS"}
