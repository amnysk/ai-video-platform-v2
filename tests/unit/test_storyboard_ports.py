"""storyboard ポートの純度（INV-6 / ADR-0016）。

``tests/unit/test_codex_adapter.py`` の script ポート検査と同じ観点を storyboard ポートへ広げる。
"""

from __future__ import annotations

import ast
import pathlib

from domain.storyboard.ports import StoryboardGenerator

REPO = pathlib.Path(__file__).resolve().parents[2]
PORTS = REPO / "domain/storyboard/ports.py"


def test_storyboard_ports_module_is_free_of_implementation_vocabulary() -> None:
    source = PORTS.read_text(encoding="utf-8").lower()
    for token in (
        "argv",
        "sandbox",
        "output-last-message",
        "turn.completed",
        "exit code",
        "subprocess",
        "codex",
        "--json",
        "openmontage",
        "scene_plan",
        "scene-director",
        "git show",
        "jsonschema",
        "workdir",
    ):
        assert token not in source, f"domain/storyboard/ports.py leaks vocabulary: {token}"


def test_storyboard_ports_module_imports_nothing_from_outer_layers() -> None:
    tree = ast.parse(PORTS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        for name in names:
            assert not name.startswith(("infrastructure", "workers", "apps"))


def test_generator_protocol_has_the_two_step_split() -> None:
    for member in (
        "generator_id",
        "generation_spec_id",
        "prepare",
        "generate",
        "interpret",
        "release",
    ):
        assert hasattr(StoryboardGenerator, member)
