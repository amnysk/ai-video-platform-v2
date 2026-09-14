"""Piper（GPL-3.0）をプラットフォームのプロセスへ読み込まない（ADR-0017 / Phase 4B）。

- ``piper`` を import してよいのは隔離 venv で子プロセスとして走る単独スクリプトだけ
  （それも ``importlib`` 経由。静的 import は一切無い）
- その単独スクリプトはプラットフォームのパッケージを import せず、誰からも import されない
- Piper adapter（実プロセス起動の入口）を import してよいファイルを完全一致で数える
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SEARCHED_DIRS = ("apps", "workers", "domain", "infrastructure", "contracts", "prompts", "tests")
PLATFORM_PACKAGES = {"apps", "workers", "domain", "infrastructure", "contracts", "prompts"}

PIPER_CLI = "infrastructure/providers/piper_cli/synthesize.py"
PIPER_ADAPTER_MODULE = "infrastructure.providers.piper_voice"
PIPER_ADAPTER_IMPORTERS = frozenset(
    {
        "workers/production_voice/run_worker.py",
        "tests/unit/test_piper_voice_adapter.py",
        "tests/live/test_piper_voice_live.py",
    }
)


def _files() -> list[pathlib.Path]:
    return [p for d in SEARCHED_DIRS for p in sorted((REPO / d).rglob("*.py"))]


def _modules(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _is(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def test_only_the_isolated_cli_script_loads_piper() -> None:
    violations = [
        f"{path.relative_to(REPO)}: loads {module}"
        for path in _files()
        if path.relative_to(REPO).as_posix() != PIPER_CLI
        for module in _modules(path)
        if _is(module, "piper")
    ]
    assert not violations, "\n".join(violations)


def test_the_cli_script_is_standalone_and_never_imported() -> None:
    cli = REPO / PIPER_CLI
    assert cli.is_file()
    leaked = {m for m in _modules(cli) if m.split(".")[0] in PLATFORM_PACKAGES}
    assert not leaked, f"{PIPER_CLI} must not import platform packages: {leaked}"
    assert not (cli.parent / "__init__.py").exists(), "piper_cli must not be an importable package"
    importers = [
        path.relative_to(REPO).as_posix()
        for path in _files()
        if any(_is(m, "infrastructure.providers.piper_cli") for m in _modules(path))
    ]
    assert not importers, importers


def test_only_sanctioned_modules_import_the_piper_adapter() -> None:
    violations = [
        rel
        for path in _files()
        if (rel := path.relative_to(REPO).as_posix())
        != f"{PIPER_ADAPTER_MODULE.replace('.', '/')}.py"
        and any(_is(m, PIPER_ADAPTER_MODULE) for m in _modules(path))
        and rel not in PIPER_ADAPTER_IMPORTERS
    ]
    assert not violations, "\n".join(violations)


def test_piper_is_not_a_dependency_of_the_platform() -> None:
    for name in ("pyproject.toml", "constraints.txt"):
        text = (REPO / name).read_text(encoding="utf-8").lower()
        assert "piper" not in text, f"{name} must not depend on piper-tts (GPL-3.0)"
