"""INV-37: Research は本番の表・本番の課金コードに触れず、Episode 本番工程は Research を待たない。

ADR-0037 は Research の永続化を本番の表から切り離した（owner-XOR を採らなかった）。その分離を
コードの形で保つ:

1. Research のモジュールは本番の課金コード（``infrastructure.production`` / ``paid_job``）と
   本番のリポジトリ（``infrastructure.db.repositories``）を import しない。本番の ORM 行
   （``JobRow`` など）も import しない
2. Research のモジュールは本番の表名（``jobs`` / ``artifact_metadata`` /
   ``provider_reservations`` / ``provider_rejections``）を文字列として持たない
   （生 SQL・FK を書けない）
3. Research の ORM 行の FK は research の表だけを指す
4. Episode 本番工程（production / render / upload / storyboard / pipeline / 課金）は Research を
   import しない（待つ・失敗させる経路をコードの上に作らない）。企画・台本側の opt-in 接続は
   ADR-0037 の後続（B6）で、既定 OFF の検査とともに扱う

理由は docs/testing/research-persistence-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Research のコード（ファイルまたはディレクトリ、REPO 相対）。後続の段で増える場所も先に並べる。
RESEARCH_PATHS: tuple[str, ...] = (
    "contracts/research.py",
    "domain/research",
    "infrastructure/research",
    "infrastructure/db/research_repositories.py",
    "infrastructure/db/migrations/versions/0015_research_foundation.py",
    "workers/research",
    "apps/api/routers/research.py",
    "infrastructure/temporal/research_starter.py",
)
#: Research のモジュール名の接頭辞（本番側が import してはならない）。
RESEARCH_MODULE_PREFIXES: tuple[str, ...] = (
    "contracts.research",
    "domain.research",
    "infrastructure.research",
    "infrastructure.db.research_repositories",
    "workers.research",
    "infrastructure.temporal.research_starter",
    "apps.api.routers.research",
)

#: Research が import してはならない本番モジュール。
FORBIDDEN_FOR_RESEARCH: tuple[str, ...] = (
    "infrastructure.production",
    "infrastructure.db.repositories",
    "infrastructure.artifact",
    "workers",
)
#: Research が import してはならない本番の ORM 行・リポジトリの名前。
FORBIDDEN_NAMES: frozenset[str] = frozenset(
    {
        "JobRow",
        "ArtifactMetadataRow",
        "ProviderReservationRow",
        "ProviderRejectionRow",
        "ProviderAuthIncidentRow",
        "ProviderReservationRepository",
        "ArtifactMetadataRepository",
        "JobRepository",
        "PaidJobRunner",
    }
)
PRODUCTION_TABLES: tuple[str, ...] = (
    "jobs",
    "artifact_metadata",
    "provider_reservations",
    "provider_rejections",
)

#: Research を import してはならない Episode 本番工程と課金のコード。
PRODUCTION_PATHS: tuple[str, ...] = (
    "workers/pipeline",
    "workers/production",
    "workers/production_image",
    "workers/production_voice",
    "workers/production_video",
    "workers/scene_alternative",
    "workers/storyboard",
    "workers/render",
    "workers/upload",
    "infrastructure/production",
    "infrastructure/providers",
    "infrastructure/artifact",
    "infrastructure/db/repositories.py",
    "infrastructure/temporal/watchdog.py",
    "domain/production",
    "domain/storyboard",
    "domain/render",
    "contracts/states.py",
    "contracts/production_activities.py",
)


def _files(paths: tuple[str, ...]) -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for rel in paths:
        path = REPO / rel
        if path.is_file():
            found.append(path)
        elif path.is_dir():
            found.extend(sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts))
    return found


def _imports(path: pathlib.Path) -> list[tuple[str, set[str]]]:
    """(モジュール名, import した名前) の列。相対 import は無い前提（repo の規約）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, set()) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.module, {alias.name for alias in node.names}))
    return found


def _under(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def _non_docstring_strings(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_the_research_persistence_modules_exist() -> None:
    """検査対象が空で自明に通る、を防ぐ。"""
    for rel in (
        "contracts/research.py",
        "domain/research/status.py",
        "infrastructure/db/research_repositories.py",
        "infrastructure/db/migrations/versions/0015_research_foundation.py",
    ):
        assert (REPO / rel).is_file(), rel


def test_research_does_not_import_production_billing_or_production_repositories() -> None:
    violations: list[str] = []
    for path in _files(RESEARCH_PATHS):
        rel = path.relative_to(REPO)
        for module, names in _imports(path):
            own_worker = rel.parts[0] == "workers" and _under(module, "workers.research")
            if not own_worker and any(_under(module, p) for p in FORBIDDEN_FOR_RESEARCH):
                violations.append(f"{rel}: imports {module}")
            for name in sorted(names & FORBIDDEN_NAMES):
                violations.append(f"{rel}: imports {name} from {module}")
    assert not violations, "INV-37:\n" + "\n".join(violations)


def test_research_code_does_not_name_production_tables() -> None:
    pattern = re.compile(r"\b(" + "|".join(PRODUCTION_TABLES) + r")\b")
    violations: list[str] = []
    for path in _files(RESEARCH_PATHS):
        for value in _non_docstring_strings(path):
            if pattern.search(value):
                violations.append(f"{path.relative_to(REPO)}: {value[:80]!r}")
    assert not violations, "INV-37:\n" + "\n".join(violations)


def test_research_rows_only_reference_research_tables() -> None:
    from infrastructure.db.models import Base

    research = {name for name in Base.metadata.tables if name.startswith("research_")}
    assert research == {"research_requests", "research_calls", "research_artifacts"}
    violations = [
        f"{name}.{fk.parent.name} -> {fk.column.table.name}"
        for name in sorted(research)
        for fk in Base.metadata.tables[name].foreign_keys
        if fk.column.table.name not in research
    ]
    assert not violations, "INV-37:\n" + "\n".join(violations)


def test_production_rows_do_not_reference_research_tables() -> None:
    """本番の表から research の表への FK を作らない（owner-XOR を採らなかった。ADR-0037）。"""
    from infrastructure.db.models import Base

    violations = [
        f"{name}.{fk.parent.name} -> {fk.column.table.name}"
        for name, table in sorted(Base.metadata.tables.items())
        if not name.startswith("research_")
        for fk in table.foreign_keys
        if fk.column.table.name.startswith("research_")
    ]
    assert not violations, "INV-37:\n" + "\n".join(violations)


def test_the_episode_production_path_does_not_import_research() -> None:
    violations: list[str] = []
    for path in _files(PRODUCTION_PATHS):
        for module, names in _imports(path):
            if any(_under(module, prefix) for prefix in RESEARCH_MODULE_PREFIXES):
                violations.append(f"{path.relative_to(REPO)}: imports {module}")
            if _under(module, "infrastructure.db.models"):
                for name in sorted(n for n in names if n.startswith("Research")):
                    violations.append(f"{path.relative_to(REPO)}: imports {name}")
    assert not violations, "INV-37:\n" + "\n".join(violations)


#: Gateway・実行器（B2）。検査対象が空で自明に通るのを防ぐ。
EXECUTION_MODULES: tuple[str, ...] = (
    "domain/research/admission.py",
    "domain/research/handlers.py",
    "infrastructure/research/gateway.py",
    "infrastructure/research/executor.py",
    "infrastructure/research/registry.py",
    "infrastructure/research/raw_store.py",
)
#: 実 Provider（実ネットワークに出る Adapter）。registry / Gateway / 実行器は組まない・import しない
#: （実 Provider の配線は所有者の判断と ADR を待つ。ADR-0037 §6）。
REAL_PROVIDER_MODULES: tuple[str, ...] = (
    "infrastructure.research.http_fetcher",
    "infrastructure.research.url_guard",
    "infrastructure.youtube.search",
)
REAL_PROVIDER_NAMES: frozenset[str] = frozenset(
    {"HttpContentFetcher", "UrlGuard", "YouTubeSearchProvider"}
)


def test_the_research_execution_modules_exist() -> None:
    for rel in EXECUTION_MODULES:
        assert (REPO / rel).is_file(), rel


def test_research_execution_does_not_wire_a_real_provider() -> None:
    """registry が知っているのは ``fake`` と ``none`` だけ。実行器は Port（注入）だけを呼ぶ。"""
    violations: list[str] = []
    for rel in EXECUTION_MODULES:
        path = REPO / rel
        for module, names in _imports(path):
            if any(_under(module, m) for m in REAL_PROVIDER_MODULES):
                violations.append(f"{rel}: imports {module}")
            for name in sorted(names & REAL_PROVIDER_NAMES):
                violations.append(f"{rel}: imports {name}")
        for value in _non_docstring_strings(path):
            if any(name in value for name in REAL_PROVIDER_NAMES):
                violations.append(f"{rel}: names {value[:80]!r}")
    assert not violations, "ADR-0037 §6:\n" + "\n".join(violations)
