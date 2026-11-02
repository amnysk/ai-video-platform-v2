"""BGM の flag の連鎖を worker の起動時に強制する（ADR-0087 §10 / INV-159）。

理由は docs/testing/bgm-auto-safety-rationale.md。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from infrastructure.music.guard import (
    require_bgm_for_acquisition,
    require_quality_gate_enforce_for_bgm,
)

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_bgm_on_without_the_enforced_gate_refuses_to_start() -> None:
    with pytest.raises(SystemExit, match="AVP_QUALITY_GATE_ENFORCE"):
        require_quality_gate_enforce_for_bgm(bgm_on=True, enforce_on=False)


@pytest.mark.parametrize(("bgm", "enforce"), [(False, False), (False, True), (True, True)])
def test_every_other_bgm_enforce_combination_starts(bgm: bool, enforce: bool) -> None:
    require_quality_gate_enforce_for_bgm(bgm_on=bgm, enforce_on=enforce)


def test_acquisition_on_without_bgm_refuses_to_start() -> None:
    with pytest.raises(SystemExit, match="BGM_ENABLED"):
        require_bgm_for_acquisition(acquisition_on=True, bgm_on=False)


@pytest.mark.parametrize(("acq", "bgm"), [(False, False), (False, True), (True, True)])
def test_every_other_acquisition_bgm_combination_starts(acq: bool, bgm: bool) -> None:
    require_bgm_for_acquisition(acquisition_on=acq, bgm_on=bgm)


@pytest.mark.parametrize("worker", ["workers/render/run_worker.py", "workers/upload/run_worker.py"])
def test_render_and_upload_workers_call_the_enforce_guard(worker: str) -> None:
    tree = ast.parse((REPO / worker).read_text(encoding="utf-8"))
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "main"]
    calls = [n for n in ast.walk(funcs[0]) if isinstance(n, ast.Call)]
    names = {c.func.id for c in calls if isinstance(c.func, ast.Name)}
    assert "require_quality_gate_enforce_for_bgm" in names, worker
