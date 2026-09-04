"""INV-16: FastAPI上で重い処理を同期実行しない / INV-1 / INV-18。"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
API = REPO / "apps" / "api"

# APIプロセスに持ち込んではいけない依存。重い処理はActivityが担う。
BANNED_IMPORT_ROOTS = {
    "minio",  # Artifact本体のI/Oはworker側
    "workers",  # UI/APIからworkerを直接呼ばない (INV-1 / INV-3)
    "subprocess",
    "multiprocessing",
}

BANNED_CALLS = {
    ("time", "sleep"),
    ("os", "system"),
}


def _api_files() -> list[pathlib.Path]:
    return sorted(API.rglob("*.py"))


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_api_does_not_import_heavy_or_worker_modules() -> None:
    violations: list[str] = []
    for path in _api_files():
        for node in ast.walk(_tree(path)):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                if name.split(".")[0] in BANNED_IMPORT_ROOTS:
                    violations.append(f"{path.relative_to(REPO)}: imports {name} (INV-16/INV-1)")
    assert not violations, "\n".join(violations)


def test_api_does_not_block_the_event_loop() -> None:
    violations: list[str] = []
    for path in _api_files():
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and (node.func.value.id, node.func.attr) in BANNED_CALLS
            ):
                violations.append(
                    f"{path.relative_to(REPO)}:{node.lineno}: "
                    f"{node.func.value.id}.{node.func.attr}() blocks the API (INV-16)"
                )
    assert not violations, "\n".join(violations)


def test_api_never_waits_for_a_workflow_result() -> None:
    """execute_workflow はworkflow完了まで待つ。APIは start_workflow だけを使う。"""
    violations: list[str] = []
    for path in _api_files():
        source = path.read_text(encoding="utf-8")
        if "execute_workflow" in source:
            violations.append(f"{path.relative_to(REPO)}: uses execute_workflow (INV-16)")
        if ".result()" in source:
            violations.append(f"{path.relative_to(REPO)}: awaits a workflow result (INV-16)")
    assert not violations, "\n".join(violations)
