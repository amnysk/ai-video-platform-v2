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


def _emitted_event_names() -> set[str]:
    """``EventName.X`` を参照している（＝発行している）event の値。"""
    from contracts.log_contract import EventName

    names: set[str] = set()
    for path in _py("apps", "workers", "infrastructure"):
        for node in ast.walk(_tree(path)):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "EventName"
                and node.attr in EventName.__members__
            ):
                names.add(EventName[node.attr].value)
    return names


def test_every_event_name_has_a_documented_emission_point() -> None:
    """発行位置の表（emission-points.md）と実コードを突き合わせる（AGENTS.md §7 の片側更新防止）。

    表で「未発行」と書いたものだけが、コードに発行箇所を持たなくてよい。
    """
    from contracts.log_contract import EventName

    doc = (REPO / "docs" / "observability" / "emission-points.md").read_text(encoding="utf-8")
    emitted = _emitted_event_names()
    rows = {line for line in doc.splitlines() if line.startswith("| `")}
    problems: list[str] = []
    for event in EventName:
        row = next((r for r in rows if f"`{event.value}`" in r.split("|")[1]), None)
        short = event.value.rsplit(".", 1)[-1]
        if row is None:
            # `reservation.reserved` / `dispatched` のように1行にまとめた行も許す
            prefix = event.value.rsplit(".", 1)[0]
            row = next(
                (r for r in rows if f"`{prefix}." in r.split("|")[1] and f"`{short}`" in r), None
            )
        if row is None:
            problems.append(f"{event.value}: emission-points.md に行が無い")
            continue
        unemitted = "未発行" in row
        if unemitted and event.value in emitted:
            problems.append(f"{event.value}: 表は未発行だがコードが発行している")
        if not unemitted and event.value not in emitted:
            problems.append(f"{event.value}: 表にあるがコードに発行箇所が無い")
    assert not problems, "\n".join(problems)


#: 発行の呼び出し。引数の組み立てごと ``with log_guard():`` の中に置く（レビュー I-2）
GUARDED_CALLS = {"emit", "defer"}


def _guarded(stack: list[ast.AST]) -> bool:
    for node in stack:
        if isinstance(node, ast.With | ast.AsyncWith) and any(
            ast.unparse(item.context_expr) == "log_guard()" for item in node.items
        ):
            return True
    return False


def test_every_emission_outside_workflows_is_guarded_with_its_arguments() -> None:
    """``emit(...)`` / ``defer(...)`` の**引数の計算**は emit の try の外で起きる。

    except 節の中の発行で引数の計算が例外を投げると、業務の例外が置き換わる（INV-38）。
    そこで発行は ``with log_guard():``（``contextlib.suppress(Exception)``）の中に置き、
    発行のための前処理（分類・行→フィールド等）も同じ block に入れる。
    ``infrastructure/logging`` 自身と Workflow（``_event`` が握る）は対象外。
    """
    bad: list[str] = []
    for path in _py("apps", "workers", "infrastructure"):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith("infrastructure/logging/"):
            continue
        source = path.read_text(encoding="utf-8")
        if "@workflow.defn" in source:
            continue

        def walk(node: ast.AST, stack: list[ast.AST], rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id in GUARDED_CALLS
                    and not _guarded(stack)
                ):
                    bad.append(f"{rel}:{child.lineno} {child.func.id}(...) outside log_guard()")
                walk(child, [*stack, child])

        walk(_tree(path), [])
    assert not bad, "\n".join(bad)


def _workflow_files() -> list[pathlib.Path]:
    return [p for p in _py("workers") if "@workflow.defn" in p.read_text(encoding="utf-8")]


def test_workflow_event_names_and_stages_are_contract_vocabulary() -> None:
    """Workflow は contracts を経ずに文字列を書く箇所がある（``stage="production"`` 等）。

    ``_event`` の event_name は ``EventName`` の値、``stage`` は ``LogStage`` の値であること
    （レビュー I-8。食い違うと Dashboards の絞り込みから黙って漏れる）。
    """
    from contracts.log_contract import EventName, LogStage
    from contracts.pipeline import PipelineStage

    stages = {s.value for s in LogStage}
    # ``stage=stage.value``（PipelineStage）を使う箇所がある
    assert {s.value for s in PipelineStage} <= stages
    bad: list[str] = []
    calls = 0
    for path in _workflow_files():
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if (
                        isinstance(key, ast.Constant)
                        and key.value == "stage"
                        and isinstance(value, ast.Constant)
                        and value.value not in stages
                    ):
                        bad.append(f"{rel}:{node.lineno} stage={value.value!r}")
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_event"
            ):
                continue
            calls += 1
            event = node.args[1] if len(node.args) > 1 else None
            names = [event] if not isinstance(event, ast.IfExp) else [event.body, event.orelse]
            for name in names:
                if not (
                    isinstance(name, ast.Attribute)
                    and ast.unparse(name.value) == "EventName"
                    and name.attr in EventName.__members__
                ):
                    bad.append(f"{rel}:{node.lineno} event={ast.unparse(event) if event else None}")
            for kw in node.keywords:
                if kw.arg != "stage":
                    continue
                if isinstance(kw.value, ast.Constant):
                    if kw.value.value not in stages:
                        bad.append(f"{rel}:{node.lineno} stage={kw.value.value!r}")
                elif ast.unparse(kw.value) != "stage.value":
                    bad.append(f"{rel}:{node.lineno} stage={ast.unparse(kw.value)}")
    assert calls >= 20
    assert not bad, "\n".join(bad)


def test_workflow_event_helpers_are_identical() -> None:
    """``_event`` は Workflow が infrastructure を import できないので各 module に置いている
    （レビュー I-8 の重複）。1つだけ直して他を忘れる片側更新を止める。"""
    bodies: dict[str, str] = {}
    for path in _workflow_files():
        for node in _tree(path).body:
            if isinstance(node, ast.FunctionDef) and node.name == "_event":
                bodies[path.relative_to(REPO).as_posix()] = ast.unparse(node)
    assert len(bodies) >= 6
    assert len(set(bodies.values())) == 1, sorted(bodies)
