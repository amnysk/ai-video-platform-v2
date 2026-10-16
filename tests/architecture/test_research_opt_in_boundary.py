"""Research への opt-in 接続の境界（INV-37、ADR-0038 §B6 / ADR-0039 §B6）。

B6 で planning worker（企画・台本）だけが Research に接続できるようになった。その接続を狭い場所に
閉じ込め、既定 OFF の経路に Research のコードが混ざらないことをコードの形で保つ:

1. ``workers/planning`` の中で Research のモジュールを import してよいのは、接続のために足した
   3 つ（``topic_trend.py`` / ``script_evidence_activities.py`` / ``research_wiring.py``）だけ
2. Workflow のコード（``workflows.py`` / ``topic_workflows.py``）と既存の Activity は、その 3 つも
   Research も import しない（Workflow が知るのは ``script_evidence.py`` の語彙と Activity 名だけ）
3. ``run_worker.py`` は ``research_wiring`` を**関数の中でだけ** import する（OFF の worker は
   Research のコードを読み込まない。実行時の確認は ``tests/unit/test_research_opt_in_off_path.py``）
4. Episode の本番工程（pipeline / storyboard / production / render / upload / 課金）が Research
   を import しないことは ``tests/architecture/test_research_isolation.py`` がそのまま検査する。ここ
   ではそれらが planning の接続モジュールも import しないことを足す

理由は docs/testing/research-opt-in-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
PLANNING = REPO / "workers" / "planning"

RESEARCH_PREFIXES: tuple[str, ...] = (
    "contracts.research",
    "contracts.research_evidence",
    "contracts.research_trend",
    "domain.research",
    "infrastructure.research",
    "infrastructure.db.research_repositories",
    "infrastructure.temporal.research_starter",
    "workers.research",
    "apps.api.routers.research",
)
#: planning の中で Research に接続してよいモジュール（B6）
LINK_MODULES: frozenset[str] = frozenset(
    {
        "workers/planning/topic_trend.py",
        "workers/planning/script_evidence_activities.py",
        "workers/planning/research_wiring.py",
    }
)
LINK_MODULE_NAMES: tuple[str, ...] = tuple(
    m.removesuffix(".py").replace("/", ".") for m in sorted(LINK_MODULES)
)
#: Workflow と、接続前からある Activity（Research も接続モジュールも import しない）
CORE_PLANNING: tuple[str, ...] = (
    "workers/planning/workflows.py",
    "workers/planning/topic_workflows.py",
    "workers/planning/activities.py",
    "workers/planning/topic_activities.py",
    "workers/planning/script_evidence.py",
)
#: Episode の本番工程（Research の import は test_research_isolation.py が禁じている）
PRODUCTION_DIRS: tuple[str, ...] = (
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
)


def _under(module: str, prefixes: tuple[str, ...]) -> bool:
    return any(module == p or module.startswith(p + ".") for p in prefixes)


def _imports(path: pathlib.Path, *, top_level_only: bool = False) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes = tree.body if top_level_only else list(ast.walk(tree))
    found: list[str] = []
    for node in nodes:
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def _rel(path: pathlib.Path) -> str:
    return str(path.relative_to(REPO))


def test_the_link_modules_exist() -> None:
    for rel in sorted(LINK_MODULES | set(CORE_PLANNING)):
        assert (REPO / rel).is_file(), rel


def test_only_the_link_modules_in_planning_import_research() -> None:
    importing = {
        _rel(path)
        for path in sorted(PLANNING.rglob("*.py"))
        if "__pycache__" not in path.parts
        and any(_under(m, RESEARCH_PREFIXES) for m in _imports(path))
    }
    assert importing == LINK_MODULES


def test_workflows_and_existing_activities_do_not_import_research_or_the_links() -> None:
    violations = [
        f"{rel}: imports {module}"
        for rel in CORE_PLANNING
        for module in _imports(REPO / rel)
        if _under(module, RESEARCH_PREFIXES) or _under(module, LINK_MODULE_NAMES)
    ]
    assert not violations, "INV-37:\n" + "\n".join(violations)


def test_run_worker_imports_the_wiring_only_lazily() -> None:
    path = PLANNING / "run_worker.py"
    top = _imports(path, top_level_only=True)
    assert not [m for m in top if _under(m, LINK_MODULE_NAMES) or _under(m, RESEARCH_PREFIXES)]
    everywhere = _imports(path)
    assert "workers.planning.research_wiring" in everywhere
    assert not [m for m in everywhere if _under(m, RESEARCH_PREFIXES)]


def test_production_stages_do_not_import_the_planning_links() -> None:
    violations = [
        f"{_rel(path)}: imports {module}"
        for rel in PRODUCTION_DIRS
        for path in sorted((REPO / rel).rglob("*.py"))
        if "__pycache__" not in path.parts
        for module in _imports(path)
        if _under(module, LINK_MODULE_NAMES) or _under(module, ("workers.planning",))
    ]
    assert not violations, "INV-37:\n" + "\n".join(violations)
