"""構造化ログの境界（ADR-0040 §3 / INV-38〜40）。理由は docs/testing/logging-rationale.md。

- 全 Worker に Activity interceptor が付く・初期化は起動点の1か所
- Workflow のモジュールは ``infrastructure`` を import しない（INV-40）
- どのモジュールも OpenSearch を import しない（アプリが書くのは stdout だけ）
- ``extra=`` のキーは ``"avp"`` だけ（LogRecord の予約属性と衝突させない）
"""

from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
APP_LAYERS = ("apps", "workers", "infrastructure", "domain", "contracts")
LOG_METHODS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _py(*layers: str) -> list[pathlib.Path]:
    return sorted(p for layer in layers for p in (REPO / layer).rglob("*.py"))


def _tree(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def _run_workers() -> list[pathlib.Path]:
    return sorted((REPO / "workers").rglob("run_worker.py"))


def test_every_worker_gets_the_activity_logging_interceptor() -> None:
    missing: list[str] = []
    count = 0
    for path in _run_workers():
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "Worker":
                count += 1
                kw = {k.arg: ast.unparse(k.value) for k in node.keywords}
                if kw.get("interceptors") != "worker_interceptors()":
                    missing.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert count >= 12
    assert not missing, missing


def test_logging_is_configured_only_at_the_entry_points() -> None:
    """basicConfig・第三者 logger のレベル設定を各 worker に散らさない（一か所へ寄せた）。"""
    offenders: list[str] = []
    for path in _py("apps", "workers", "infrastructure"):
        if path.parent.name == "logging" and path.parent.parent.name == "infrastructure":
            continue
        source = path.read_text(encoding="utf-8")
        if "basicConfig(" in source or 'getLogger("httpx").setLevel' in source:
            offenders.append(str(path.relative_to(REPO)))
    assert not offenders, offenders
    entry = (REPO / "infrastructure" / "runtime" / "worker_entry.py").read_text(encoding="utf-8")
    assert "configure_logging(" in entry
    serve = (REPO / "apps" / "api" / "serve.py").read_text(encoding="utf-8")
    assert "configure_logging(" in serve


def test_workflow_modules_do_not_import_infrastructure() -> None:
    """INV-40: Workflow は workflow.logger と extra={"avp": ...} だけ。sandbox に infrastructure を
    持ち込まない（sandbox 内で再 import されると handler が分裂し workflow task が失敗する）。"""
    workflow_files = [
        p for p in _py("workers") if "@workflow.defn" in p.read_text(encoding="utf-8")
    ]
    assert len(workflow_files) >= 9
    bad = [
        f"{p.relative_to(REPO)}: {name}"
        for p in workflow_files
        for name in _imports(_tree(p))
        if name.split(".")[0] in {"infrastructure", "apps"}
    ]
    assert not bad, bad


def test_nothing_imports_opensearch() -> None:
    bad = [
        f"{p.relative_to(REPO)}: {name}"
        for p in _py(*APP_LAYERS)
        for name in _imports(_tree(p))
        if "opensearch" in name.lower() or name.split(".")[0] in {"elasticsearch", "fluent"}
    ]
    assert not bad, bad


def test_log_extra_uses_only_the_avp_key() -> None:
    """``workflow.logger.*(…, extra=…)`` のキーは ``"avp"`` だけ。Workflow の外では ``extra=`` を
    直接書かず ``emit()`` を使う（emit は record の生成を含めて例外を握る。直接の
    ``logger.warning(extra=...)`` はロガーの故障を業務へ伝播させる。INV-38 の故障注入で実測）。"""
    bad: list[str] = []
    for path in _py("apps", "workers", "infrastructure"):
        is_workflow = "@workflow.defn" in path.read_text(encoding="utf-8")
        for node in ast.walk(_tree(path)):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in LOG_METHODS
            ):
                continue
            for kw in node.keywords:
                if kw.arg != "extra":
                    continue
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                if ast.unparse(kw.value) == "{RECORD_EXTRA_KEY: payload}":
                    continue  # infrastructure.logging.emit（RECORD_EXTRA_KEY == "avp"）
                if not is_workflow:
                    bad.append(f"{where} extra= outside a workflow (use emit())")
                    continue
                if isinstance(kw.value, ast.Dict) and all(
                    isinstance(k, ast.Constant) for k in kw.value.keys
                ):
                    keys = [k.value for k in kw.value.keys if isinstance(k, ast.Constant)]
                    if keys != ["avp"]:
                        bad.append(f"{where} keys={keys}")
                else:
                    bad.append(f"{where} extra={ast.unparse(kw.value)}")
    assert not bad, bad
