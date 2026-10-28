"""地図の描画・判定部品の境界（ADR-0078、INV-119 / INV-120）。

1. 地図の部品は有料 provider（fal 等）・ネットワーク・時計・乱数・子プロセスを import しない。
2. 地図の描画部品（infrastructure/media/map_*.py）を、本番の workflow・Activity・storyboard・
   production・render の計画（identity）側が import しない（flag OFF で出力が変わらない・未接続）。

理由は docs/testing/geo-maps-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

MAP_MODULES: tuple[pathlib.Path, ...] = (
    REPO / "contracts" / "geo_map.py",
    REPO / "domain" / "geo" / "map_accuracy.py",
    REPO / "domain" / "geo" / "map_presentation.py",
    REPO / "domain" / "geo" / "map_projection.py",
    REPO / "infrastructure" / "media" / "map_render.py",
    REPO / "infrastructure" / "media" / "map_clip.py",
    REPO / "infrastructure" / "media" / "map_scene_visual.py",
)
#: 有料 provider・ネットワーク・時計・乱数・子プロセス・並行。
FORBIDDEN_ROOTS = frozenset(
    {
        "httpx",
        "requests",
        "aiohttp",
        "urllib",
        "http",
        "socket",
        "ssl",
        "subprocess",
        "asyncio",
        "threading",
        "random",
        "secrets",
        "uuid",
        "time",
        "datetime",
        "temporalio",
        "sqlalchemy",
        "workers",
        "apps",
        "fal_client",
        "openai",
        "anthropic",
        "importlib",
    }
)
FORBIDDEN_INFRA = ("infrastructure.providers", "infrastructure.youtube", "infrastructure.analytics")

#: 地図の描画を本番側が import してはならない場所。
PRODUCTION_SIDE: tuple[str, ...] = (
    "workers",
    "apps",
    "domain/storyboard",
    "domain/production",
    "domain/render",
    "contracts/states.py",
    "contracts/production_activities.py",
    "contracts/render_activities.py",
    "infrastructure/render",
    "infrastructure/providers",
    "infrastructure/media/normalize.py",
    "infrastructure/media/probe.py",
)
MAP_PREFIXES = ("infrastructure.media.map_", "domain.geo.map_", "contracts.geo_map")


def _package_of(path: pathlib.Path) -> str:
    """ファイルの属するパッケージ（相対 import の解決用）。"""
    return ".".join(path.relative_to(REPO).parent.parts)


def _modules_of_source(source: str, package: str = "") -> list[str]:
    """import されうる全てのモジュール名。

    ``from a import b`` は ``a`` と ``a.b`` の両方（b がサブモジュールのことがある）、相対 import は
    ``package`` で絶対名に直し、``import_module("x")`` / ``__import__("x")`` の定数引数も拾う。
    """
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


def _violations(source: str, package: str = "") -> list[str]:
    out: list[str] = []
    for module in _modules_of_source(source, package):
        if module.split(".")[0] in FORBIDDEN_ROOTS or module.startswith(FORBIDDEN_INFRA):
            out.append(module)
    return out


def _py(path: pathlib.Path) -> list[pathlib.Path]:
    if path.is_file():
        return [path]
    return sorted(p for p in path.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_map_modules_exist() -> None:
    """検査対象が空で自明に通るのを防ぐ。"""
    names = {p.name for p in MAP_MODULES}
    assert {
        "geo_map.py",
        "map_accuracy.py",
        "map_presentation.py",
        "map_render.py",
        "map_clip.py",
    } <= names
    assert all(p.is_file() for p in MAP_MODULES)


def test_map_modules_import_no_paid_provider_network_clock_or_random() -> None:
    violations = [
        f"{p.relative_to(REPO)}: imports {m}"
        for p in MAP_MODULES
        for m in _violations(p.read_text(encoding="utf-8"), _package_of(p))
    ]
    assert not violations, "ADR-0078 (INV-119):\n" + "\n".join(violations)


def test_the_detector_catches_violating_fixtures() -> None:
    assert _violations("from infrastructure.providers.fal_queue import FalQueueClient")
    assert _violations("import httpx")
    assert _violations("import random")
    assert _violations("from datetime import datetime")
    assert _violations("import subprocess")
    assert not _violations(
        "from PIL import Image\nimport io\nfrom contracts.geo import GeoManifest"
    )


def test_the_map_modules_do_not_call_clock_or_random_builtins() -> None:
    bad: list[str] = []
    for p in MAP_MODULES:
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"open", "input", "print", "exec", "eval", "__import__"}
            ):
                bad.append(f"{p.relative_to(REPO)}:{node.lineno}: {node.func.id}()")
    assert not bad, "\n".join(bad)


def test_production_side_does_not_import_the_map_parts() -> None:
    violations: list[str] = []
    for rel in PRODUCTION_SIDE:
        for p in _py(REPO / rel):
            for module in _modules_of_source(p.read_text(encoding="utf-8"), _package_of(p)):
                if module.startswith(MAP_PREFIXES):
                    violations.append(f"{p.relative_to(REPO)}: imports {module}")
    assert not violations, "ADR-0078 (INV-120, not wired):\n" + "\n".join(violations)


# --- 検出器の fixture（独立レビュー MAJOR 2）


def test_the_detector_sees_from_package_import_submodule() -> None:
    assert _violations("from infrastructure import providers")


def test_the_detector_resolves_relative_imports() -> None:
    assert _violations("from ..providers import fal_queue", "infrastructure.media")
    assert _violations("from .. import providers", "infrastructure.media")
    assert not _violations("from . import map_render", "infrastructure.media")


def test_the_detector_sees_dynamic_imports() -> None:
    dynamic = 'import importlib\nimportlib.import_module("infrastructure.providers.fal")'
    assert _violations(dynamic)
    assert _violations('__import__("infrastructure.providers.fal_queue")')
    assert "infrastructure.providers.fal" in _modules_of_source(dynamic)


def test_the_production_side_scan_sees_the_map_parts_in_every_import_form() -> None:
    forms = [
        ("from infrastructure.media import map_render", ""),
        ("from domain.geo import map_accuracy", ""),
        ("from contracts import geo_map", ""),
        ("from .map_render import render_map", "infrastructure.media"),
        ("from . import map_clip", "infrastructure.media"),
        ('import importlib\nimportlib.import_module("infrastructure.media.map_render")', ""),
    ]
    for source, package in forms:
        modules = _modules_of_source(source, package)
        assert any(m.startswith(MAP_PREFIXES) for m in modules), (source, modules)
    assert not any(
        m.startswith(MAP_PREFIXES)
        for m in _modules_of_source("from infrastructure.media import probe", "")
    )
