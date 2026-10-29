"""BGM ON なのに説明欄の構成が OFF だと起動しない（ADR-0086 §7 / INV-148）。理由は docs/testing/v3-phase3-wiring.md。"""  # noqa: E501

from __future__ import annotations

import ast
import pathlib

import pytest

from infrastructure.music.guard import require_description_composition_for_bgm

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_bgm_on_with_composition_off_refuses_to_start() -> None:
    with pytest.raises(SystemExit, match="AVP_UPLOAD_DESCRIPTION_COMPOSED"):
        require_description_composition_for_bgm(bgm_on=True, composition_enabled=False)


@pytest.mark.parametrize(("bgm", "composed"), [(False, False), (False, True), (True, True)])
def test_every_other_combination_starts(bgm: bool, composed: bool) -> None:
    require_description_composition_for_bgm(bgm_on=bgm, composition_enabled=composed)


@pytest.mark.parametrize("worker", ["workers/render/run_worker.py", "workers/upload/run_worker.py"])
def test_both_workers_call_the_guard(worker: str) -> None:
    tree = ast.parse((REPO / worker).read_text(encoding="utf-8"))
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"]
    calls = [n for n in ast.walk(funcs[0]) if isinstance(n, ast.Call)]
    names = {c.func.id for c in calls if isinstance(c.func, ast.Name)}
    assert "require_description_composition_for_bgm" in names, worker
