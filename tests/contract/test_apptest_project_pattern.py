"""隔離スタックの project 名の規則は 1 箇所（contracts/apptest.py。AGENTS.md §8）。

run-e2e.sh（シェル）は Python を import できないので、同じ接頭辞を書いている。食い違うと、guard が
通す project と run-e2e.sh が起動する project がずれる（本番 ``avp2`` を誤って通す・隔離を拒む）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from contracts.apptest import APPTEST_PROJECT_PREFIX, APPTEST_PROJECT_RE

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "name", ["avp2-oslog-c", "avp2-oslog-c-p3a", "avp2-oslog-c_1", "avp2-oslog-c9"]
)
def test_isolated_project_names_match(name: str) -> None:
    assert APPTEST_PROJECT_RE.fullmatch(name)


@pytest.mark.parametrize(
    "name", ["avp2", "avp2-oslog", "avp2-oslog-cX", "xavp2-oslog-c", "avp2-oslog-c ", ""]
)
def test_the_production_project_and_lookalikes_never_match(name: str) -> None:
    assert not APPTEST_PROJECT_RE.fullmatch(name)


def test_run_e2e_uses_the_same_prefix_and_pattern() -> None:
    script = (ROOT / "deploy/logging/test/run-e2e.sh").read_text(encoding="utf-8")
    assert f"{APPTEST_PROJECT_PREFIX}|{APPTEST_PROJECT_PREFIX}[-_0-9a-z]*)" in script


def test_the_guard_imports_the_pattern_instead_of_redefining_it() -> None:
    guard = (ROOT / "tests/support/production_guard.py").read_text(encoding="utf-8")
    assert "from contracts.apptest import APPTEST_PROJECT_RE" in guard
    assert "oslog-c" not in guard.replace("avp2-oslog-c*", "")
