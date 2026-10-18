"""Research tool adapter の境界（ADR-0047 §4、INV-37 / INV-60）。

``infrastructure/agents/tools/research.py`` は Research を呼ぶ側（adapter）で、Research そのものでは
ない。既存の隔離の規則は緩めず、adapter にも同じ制約を課す:

1. 本番の課金・Artifact・リポジトリ（``infrastructure.production`` / ``infrastructure.artifact`` /
   ``infrastructure.db.repositories``）と本番の ORM 行を import しない・本番の表名を持たない
2. 実 Provider（``HttpContentFetcher`` / ``UrlGuard`` / ``YouTubeSearchProvider``）を import しない
   （任意 URL 取得の経路を台帳の外に作らない。``fetch_sources`` は参照ビューだけ）
3. Episode 本番工程は adapter を import しない（Research を待つ経路をコードの上に作らない）

理由: docs/testing/agent-tools-rationale.md
"""

from __future__ import annotations

import re

from tests.architecture.test_research_isolation import (
    FORBIDDEN_FOR_RESEARCH,
    FORBIDDEN_NAMES,
    PRODUCTION_PATHS,
    PRODUCTION_TABLES,
    REAL_PROVIDER_MODULES,
    REAL_PROVIDER_NAMES,
    REPO,
    _files,
    _imports,
    _non_docstring_strings,
    _under,
)

ADAPTER = "infrastructure/agents/tools/research.py"


def test_the_adapter_exists() -> None:
    assert (REPO / ADAPTER).is_file()


def test_the_adapter_follows_the_research_isolation_rules() -> None:
    path = REPO / ADAPTER
    violations: list[str] = []
    for module, names in _imports(path):
        if any(_under(module, p) for p in FORBIDDEN_FOR_RESEARCH):
            violations.append(f"imports {module}")
        if any(_under(module, m) for m in REAL_PROVIDER_MODULES):
            violations.append(f"imports real provider {module}")
        violations += [f"imports {n} from {module}" for n in sorted(names & FORBIDDEN_NAMES)]
        violations += [f"imports {n} from {module}" for n in sorted(names & REAL_PROVIDER_NAMES)]
    pattern = re.compile(r"\b(" + "|".join(PRODUCTION_TABLES) + r")\b")
    violations += [
        f"names a production table: {v[:60]!r}"
        for v in _non_docstring_strings(path)
        if pattern.search(v)
    ]
    assert not violations, "INV-37 / ADR-0047 §4:\n" + "\n".join(violations)


def test_episode_production_does_not_import_the_agent_tool_adapters() -> None:
    violations = [
        f"{path.relative_to(REPO)}: imports {module}"
        for path in _files(PRODUCTION_PATHS)
        for module, _ in _imports(path)
        if _under(module, "infrastructure.agents.tools")
    ]
    assert not violations, "INV-37:\n" + "\n".join(violations)
