"""INV-18: テストとCIから有料API・実投稿へ到達しうる依存を持ち込まない。"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]

# Phase 1 では有料provider/YouTubeの実装自体を持たない。
FORBIDDEN_TOKENS = {
    "fal.run": "fal.ai endpoint",
    "fal.ai": "fal.ai",
    "queue.fal": "fal.ai queue",
    "googleapis.com/youtube": "YouTube API",
    "youtube.googleapis.com": "YouTube API",
    "api.openai.com": "OpenAI",
    "api.anthropic.com": "Anthropic",
}

SEARCHED_DIRS = ["apps", "workers", "domain", "infrastructure", "contracts", "tests"]
SELF = pathlib.Path(__file__).resolve()


def test_no_live_provider_endpoints_in_the_codebase() -> None:
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            if path.resolve() == SELF:
                continue
            source = path.read_text(encoding="utf-8")
            for token, label in FORBIDDEN_TOKENS.items():
                if token in source:
                    violations.append(f"{path.relative_to(REPO)}: mentions {label} (INV-18)")
    assert not violations, "\n".join(violations)


# --- live テストの隔離（AGENTS.md §9） ------------------------------------

import ast  # noqa: E402
import re  # noqa: E402
import tomllib  # noqa: E402

LIVE_SWITCH = "AVP_LIVE_CODEX"
CODEX_ADAPTER_MODULE = "infrastructure.providers.codex_cli"


def _pytest_ini_options() -> dict:
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    return data["tool"]["pytest"]["ini_options"]


def test_live_tests_are_excluded_from_the_default_pytest_run() -> None:
    """引数なしの ``pytest`` が本物の Codex を呼ばないこと。"""
    options = _pytest_ini_options()
    markers = " ".join(options.get("markers", []))
    assert "live:" in markers, "live マーカーが pyproject.toml に登録されていない"
    addopts = options.get("addopts", "")
    addopts = " ".join(addopts) if isinstance(addopts, list) else addopts
    assert "not live" in addopts.replace("'", "").replace('"', "")


def test_live_conftest_refuses_to_import_without_the_env_switch() -> None:
    source = (REPO / "tests/live/conftest.py").read_text(encoding="utf-8")
    assert "collect_ignore_glob" in source
    assert LIVE_SWITCH in source


def test_no_ci_workflow_enables_the_live_codex_switch() -> None:
    """CI が live スイッチを **有効化** しないこと。

    ``AVP_LIVE_CODEX: ""`` のように**塞ぐ**記述は違反ではない（むしろ望ましい）。
    禁じるのは真値を入れること、``tests/live`` を走らせること、``-m live`` を使うこと。
    """
    enabling = re.compile(
        rf"""(
            {LIVE_SWITCH}\s*[:=]\s*["']?(?!["']?\s*(?:$|\n))(?!""|'')\S
          | (?:pytest|python\s+-m\s+pytest)[^\n]*tests/live
          | -m\s+["']?live
        )""",
        re.VERBOSE,
    )
    hits: list[str] = []
    workflows = REPO / ".github" / "workflows"
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if enabling.search(line):
                hits.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()} (INV-18)")
    assert not hits, "\n".join(hits)


def _imports_module(path: pathlib.Path, module: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == module or a.name.startswith(module + ".") for a in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and (node.module == module or node.module.startswith(module + "."))
        ):
            return True
    return False


def test_only_sanctioned_modules_import_the_codex_cli_adapter() -> None:
    """有料/実CLI呼び出しの入口を数えられる場所に限る。"""
    violations: list[str] = []
    for directory in SEARCHED_DIRS:
        for path in sorted((REPO / directory).rglob("*.py")):
            rel = path.relative_to(REPO)
            if not _imports_module(path, CODEX_ADAPTER_MODULE):
                continue
            sanctioned = (
                (rel.parts[0] == "workers" and rel.name == "run_worker.py")
                or rel.parts[:2] == ("tests", "live")
                or rel.as_posix() == "tests/unit/test_codex_adapter.py"
                or rel.as_posix() == f"{CODEX_ADAPTER_MODULE.replace('.', '/')}.py"
            )
            if not sanctioned:
                violations.append(f"{rel}: imports {CODEX_ADAPTER_MODULE} (INV-18)")
    assert not violations, "\n".join(violations)
