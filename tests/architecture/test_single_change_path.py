"""INV-50: StoryboardPatchProposal から propose_change を作るのは ``ProposeStoryboardChange`` だけ。

HTTP・アシスタント・内部 Agent が提案を use case へ通すだけで、変換を持たないことを検査する。
edit ごとの command_id の導出を自前で持たない。理由は docs/testing/jpmas2-p0b-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
LAYERS = ("apps", "application", "contracts", "domain", "infrastructure", "scripts", "workers")
USE_CASE = "application/use_cases/propose_storyboard_change.py"
DEFINITION = "contracts/application.py"
#: 提案の型を import してよい module。型を受け渡す／schema を導出するだけの場所
PASS_THROUGH = {
    USE_CASE,
    DEFINITION,
    "contracts/assistant_tools.py",  # tool の入力 schema の導出
    "apps/api/routers/application.py",  # HTTP の本文の型
    "domain/agents/runtime.py",  # AgentOutput.proposals の型（変換はしない）
    "domain/agents/handlers/quality.py",  # 提案を組み立てて返すだけ（command への変換はしない）
}
PROPOSAL_NAMES = {"StoryboardPatchProposal", "StoryboardSceneEdit"}


def _names(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
    return found


def _modules() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for layer in LAYERS:
        for path in sorted((REPO / layer).rglob("*.py")):
            out[str(path.relative_to(REPO))] = _names(path)
    return out


def test_only_pass_through_modules_know_the_proposal_types() -> None:
    offenders = [
        m for m, names in _modules().items() if names & PROPOSAL_NAMES and m not in PASS_THROUGH
    ]
    assert offenders == [], f"{offenders}: use ProposeStoryboardChange instead (INV-50)"


def test_only_the_use_case_turns_a_proposal_into_scene_overrides() -> None:
    """proposal の型と ``SceneOverrideVisual`` を同じ module で扱うのは use case だけ。"""
    both = [
        m
        for m, names in _modules().items()
        if names & PROPOSAL_NAMES
        and "SceneOverrideVisual" in names
        and m not in {USE_CASE, DEFINITION}
    ]
    assert both == [], both


def test_only_the_use_case_derives_edit_command_ids() -> None:
    users = [
        m
        for m, names in _modules().items()
        if "storyboard_edit_command_id" in names and m not in {USE_CASE, DEFINITION}
    ]
    assert users == [], users
    assert "storyboard_edit_command_id" in _modules()[USE_CASE]
