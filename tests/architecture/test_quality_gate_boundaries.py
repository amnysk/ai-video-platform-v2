"""INV-101〜103 の形の検査: 品質判定と tool 契約は純粋で、許可リストに禁止 tool を入れない。

1. ``contracts/quality.py`` / ``contracts/agent_tools.py`` / ``domain/quality`` は I/O をせず、
   provider adapter・YouTube adapter・``ProviderReservationRepository``・workers を import しない
2. 禁止 tool id が ``AllowedTool``（許可リスト）に現れない

検査器そのものも、違反する fixture を実際に捕まえることを別テストで示す（検査の検査）。
理由は docs/testing/quality-gate-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from contracts.agent_tools import ALLOWED_TOOL_IDS, FORBIDDEN_TOOL_IDS

REPO = pathlib.Path(__file__).resolve().parents[2]

PURE_PATHS: tuple[str, ...] = (
    "contracts/quality.py",
    "contracts/agent_tools.py",
    "domain/quality",
)

#: import してはならないモジュールの接頭辞（provider・YouTube・DB・I/O・Temporal・上位層）。
FORBIDDEN_MODULES: tuple[str, ...] = (
    "infrastructure",
    "workers",
    "apps",
    "sqlalchemy",
    "temporalio",
    "httpx",
    "requests",
    "urllib",
    "socket",
    "subprocess",
    "os",
    "shutil",
    "pathlib",
    "tempfile",
    "asyncio",
    "boto3",
    "minio",
    "fal_client",
    "googleapiclient",
    "google",
)
#: 名前として現れてはならないもの（別経路の import や参照でも拾う）。
FORBIDDEN_NAMES: frozenset[str] = frozenset({"ProviderReservationRepository", "open", "print"})


def _modules_of(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [a.name for a in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        return [node.module]
    return []


def io_violations(source: str) -> list[str]:
    """純粋であるべきソースの違反を返す。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        for module in _modules_of(node):
            if any(module == m or module.startswith(m + ".") for m in FORBIDDEN_MODULES):
                found.append(f"imports {module}")
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES:
                    found.append(f"imports name {alias.name}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"uses name {node.id}")
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_NAMES:
            found.append(f"uses attribute {node.attr}")
    return found


def allowlist_violations(source: str) -> list[str]:
    """``AllowedTool`` の本体に禁止 tool id の文字列が現れていないか。"""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == "AllowedTool":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Constant) and inner.value in FORBIDDEN_TOOL_IDS:
                    found.append(f"AllowedTool contains {inner.value}")
    return found


def _pure_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for rel in PURE_PATHS:
        path = REPO / rel
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


def test_the_pure_files_exist() -> None:
    names = {p.relative_to(REPO).as_posix() for p in _pure_files()}
    assert {"contracts/quality.py", "contracts/agent_tools.py", "domain/quality/aggregate.py"} <= (
        names
    )


def test_quality_and_agent_tool_modules_do_no_io_and_import_no_provider_or_worker() -> None:
    violations = [
        f"{p.relative_to(REPO)}: {v} (INV-101/103)"
        for p in _pure_files()
        for v in io_violations(p.read_text(encoding="utf-8"))
    ]
    assert not violations, "\n".join(violations)


@pytest.mark.parametrize(
    "source",
    [
        "from infrastructure.providers.fal_image import X",
        "import infrastructure.youtube.uploader",
        "from infrastructure.db.repositories import ProviderReservationRepository",
        "from workers.pipeline import activities",
        "import httpx",
        "import os",
        "from pathlib import Path",
        "def f():\n    return open('x').read()",
        "import importlib\nProviderReservationRepository",
        "import sqlalchemy.orm",
    ],
)
def test_the_io_detector_catches_violating_fixtures(source: str) -> None:
    assert io_violations(source), source


def test_the_io_detector_accepts_a_clean_module() -> None:
    assert not io_violations("from contracts.quality import QualityVerdict\nimport json\n")


def test_forbidden_tool_ids_are_not_in_the_allowlist() -> None:
    assert not (ALLOWED_TOOL_IDS & FORBIDDEN_TOOL_IDS)
    source = (REPO / "contracts/agent_tools.py").read_text(encoding="utf-8")
    assert not allowlist_violations(source), allowlist_violations(source)


def test_the_forbidden_set_covers_billing_upload_migrate_schedule_and_reservation_release() -> None:
    assert {
        "fal.generate",
        "youtube.upload",
        "db.migrate",
        "schedule.pause",
        "provider_reservations.release",
    } == FORBIDDEN_TOOL_IDS


def test_the_allowlist_detector_catches_a_violating_fixture() -> None:
    bad = (
        "from enum import StrEnum\n"
        "class AllowedTool(StrEnum):\n"
        "    A = 'research.search'\n"
        "    B = 'youtube.upload'\n"
    )
    assert allowlist_violations(bad) == ["AllowedTool contains youtube.upload"]
    good = "class AllowedTool:\n    A = 'research.search'\n"
    assert not allowlist_violations(good)
