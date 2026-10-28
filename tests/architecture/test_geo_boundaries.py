"""domain/geo の境界（ADR-0071、INV-6 の追加の枠）。

1. domain/geo は I/O・時計・乱数・ネットワークに触れない（純関数。同じ入力から同じ結果）。
2. domain/geo と contracts/geo を、本番の課金・Episode 工程・API・workflow が import しない
   （この枝は未接続。接続は後続の枝が ADR とともに決める）。
3. domain/geo は ``contracts`` 以外の自分のレイヤ外を import しない。

理由は docs/testing/geo-contracts-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

GEO_DOMAIN = REPO / "domain" / "geo"
GEO_PREFIXES = ("domain.geo", "contracts.geo", "contracts.geo_map")

#: geo を import してよい infrastructure の唯一の場所（ADR-0078。描画部品。workflow からは未接続）。
GEO_CONSUMER_EXEMPT = (
    REPO / "infrastructure" / "media" / "map_render.py",
    REPO / "infrastructure" / "media" / "map_clip.py",
    REPO / "infrastructure" / "media" / "map_scene_visual.py",
)

#: 純関数に不要な標準・外部のモジュール（I/O・時計・乱数・ネットワーク・並行）。
IMPURE_MODULES = frozenset(
    {
        "os",
        "io",
        "sys",
        "socket",
        "ssl",
        "http",
        "urllib",
        "subprocess",
        "shutil",
        "tempfile",
        "pathlib",
        "glob",
        "sqlite3",
        "pickle",
        "time",
        "datetime",
        "random",
        "secrets",
        "uuid",
        "asyncio",
        "threading",
        "multiprocessing",
        "logging",
        "requests",
        "httpx",
        "aiohttp",
        "temporalio",
        "sqlalchemy",
    }
)
IMPURE_CALLS = frozenset({"open", "input", "print", "exec", "eval", "__import__"})

#: geo を import してはならない本番の課金・Episode 工程・入口（未接続を守る）。
CONSUMERS_FORBIDDEN: tuple[str, ...] = (
    "workers",
    "apps",
    "infrastructure",
    "contracts/states.py",
    "contracts/production_activities.py",
    "domain/production",
    "domain/storyboard",
    "domain/render",
    "domain/research",
)


def _py(path: pathlib.Path) -> list[pathlib.Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def _modules(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_the_geo_domain_exists() -> None:
    """検査対象が空で自明に通るのを防ぐ。"""
    assert {p.name for p in _py(GEO_DOMAIN)} >= {"verify.py", "combine.py", "tolerance.py"}
    assert (REPO / "contracts" / "geo.py").is_file()


def test_the_geo_domain_imports_no_io_clock_or_network_modules() -> None:
    violations = [
        f"{p.relative_to(REPO)}: imports {module}"
        for p in _py(GEO_DOMAIN)
        for module in _modules(p)
        if module.split(".")[0] in IMPURE_MODULES
    ]
    assert not violations, "ADR-0071:\n" + "\n".join(violations)


def test_the_geo_domain_does_not_call_io_builtins() -> None:
    violations: list[str] = []
    for p in _py(GEO_DOMAIN):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in IMPURE_CALLS
            ):
                violations.append(f"{p.relative_to(REPO)}:{node.lineno}: {node.func.id}()")
    assert not violations, "ADR-0071:\n" + "\n".join(violations)


def test_the_geo_domain_imports_only_contracts_and_itself() -> None:
    allowed = ("contracts", "domain.geo")
    violations = [
        f"{p.relative_to(REPO)}: imports {module}"
        for p in _py(GEO_DOMAIN)
        for module in _modules(p)
        if module.split(".")[0] in {"contracts", "domain", "infrastructure", "workers", "apps"}
        and not module.startswith(allowed)
    ]
    assert not violations, "INV-6:\n" + "\n".join(violations)


def test_production_billing_and_entrypoints_do_not_import_geo() -> None:
    violations: list[str] = []
    for rel in CONSUMERS_FORBIDDEN:
        for p in _py(REPO / rel):
            if any(p.is_relative_to(exempt) for exempt in GEO_CONSUMER_EXEMPT):
                continue
            for module in _modules(p):
                if any(module == g or module.startswith(g + ".") for g in GEO_PREFIXES):
                    violations.append(f"{p.relative_to(REPO)}: imports {module}")
    assert not violations, "ADR-0071 (not wired yet):\n" + "\n".join(violations)
