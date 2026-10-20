"""release preflight の純粋な判定（ADR-0050 / INV-65）。

I/O は一切しない。外界は ``Collectors`` Protocol として注入され、collector が投げた例外・不在は
**すべて UNKNOWN** に変換する。UNKNOWN も欠けた必須チェックも READY にはならない。
実際の collector は ``infrastructure/release/collectors.py``（読み取りだけ）、CLI は
``scripts/release-preflight.py``。
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import re
import tokenize
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

from contracts.ci import CI_GATE_JOB_NAME
from contracts.release import (
    STAGING_DB_NAME,
    STAGING_ENVIRONMENT_MARK,
    STAGING_TEMPORAL_NAMESPACE,
    TEST_DB_SUFFIX,
)

# ------------------------------------------------------------------ 語彙


class Status(StrEnum):
    PASS = "PASS"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class Overall(StrEnum):
    READY = "READY"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class Relation(StrEnum):
    """deployed -> target の関係。"""

    SAME = "same"
    FAST_FORWARD = "fast_forward"
    BEHIND = "behind"
    DIVERGED = "diverged"
    UNKNOWN_COMMIT = "unknown_commit"


EXIT_CODES: Mapping[Overall, int] = {Overall.READY: 0, Overall.BLOCKED: 2, Overall.UNKNOWN: 3}

#: deploy が焼く revision label（tests/contract/test_deploy_workers.py と同じ値）
REVISION_LABEL = "org.opencontainers.image.revision"
#: この repo からビルドするアプリイメージ（workers-versions.sh と同じ集合）
APP_IMAGES = ("avp2-app:local", "avp2-worker:local")
COMPOSE_PROJECT = "avp2"
BACKUP_COMPONENTS = ("db_dump", "minio", "temporal")
DEFAULT_BACKUP_MAX_AGE_HOURS = 24.0

CHECK_TARGET_SHA = "target_sha"
CHECK_TARGET_ENV = "target_environment"
CHECK_WORKTREE = "worktree_clean"
CHECK_ANCESTRY = "ancestry"
CHECK_CI = "ci"
CHECK_CONTAINERS = "container_revisions"
CHECK_ALEMBIC = "alembic"
CHECK_MIGRATION = "migration_compat"
CHECK_RUNNING = "running_workflows"
CHECK_RESERVATIONS = "provider_reservations"
CHECK_SCHEDULES = "schedule_snapshot"
CHECK_BACKUP = "backup_freshness"
CHECK_REPLAY = "replay_evidence"
CHECK_APPROVAL = "owner_approval"

CHECK_AT_TARGET = "containers_at_target"
CHECK_POLLERS = "worker_pollers"
CHECK_SCHEDULES_SAME = "schedules_unchanged"
CHECK_ALEMBIC_HEAD = "alembic_at_head"

PRE_DEPLOY_REQUIRED: tuple[str, ...] = (
    CHECK_TARGET_SHA,
    CHECK_TARGET_ENV,
    CHECK_WORKTREE,
    CHECK_ANCESTRY,
    CHECK_CI,
    CHECK_CONTAINERS,
    CHECK_ALEMBIC,
    CHECK_MIGRATION,
    CHECK_RUNNING,
    CHECK_RESERVATIONS,
    CHECK_SCHEDULES,
    CHECK_BACKUP,
    CHECK_REPLAY,
    CHECK_APPROVAL,
)
POST_DEPLOY_REQUIRED: tuple[str, ...] = (
    CHECK_TARGET_SHA,
    CHECK_TARGET_ENV,
    CHECK_AT_TARGET,
    CHECK_POLLERS,
    CHECK_SCHEDULES_SAME,
    CHECK_ALEMBIC_HEAD,
)

#: 本番の DB / Temporal を読んで判定するチェック。対象の取り違えに気づけるよう detail に宛先を出す
TARGET_BOUND_CHECKS = frozenset(
    {
        "alembic",
        "running_workflows",
        "provider_reservations",
        "schedule_snapshot",
        "worker_pollers",
        "schedules_unchanged",
        "alembic_at_head",
    }
)

_SHA = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HeadState:
    head_sha: str
    dirty: bool


@dataclass(frozen=True)
class CheckRun:
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True)
class TargetInfo:
    """この preflight が読みに行く DB / Temporal の宛先（資格情報は持たない）。"""

    db_host: str
    db_name: str
    temporal_address: str
    temporal_namespace: str
    environment: str


@dataclass(frozen=True)
class Request:
    target_sha: str
    allow_non_ff: bool = False
    backup_manifest: str | None = None
    backup_max_age_hours: float = DEFAULT_BACKUP_MAX_AGE_HOURS
    replay_evidence: str | None = None
    approval_file: str | None = None
    verify_post_deploy: bool = False
    pre_report: str | None = None


@dataclass(frozen=True)
class Report:
    target_sha: str
    mode: str
    required: tuple[str, ...]
    results: tuple[CheckResult, ...]
    overall: Overall


class CollectorUnavailable(Exception):
    """collector が値を確認できなかった（届かない・権限が無い等）。UNKNOWN になる。"""


class Collectors(Protocol):
    def commit_exists(self, sha: str) -> bool: ...
    def head_state(self) -> HeadState: ...
    def container_revisions(self) -> Mapping[str, str | None]: ...
    def ancestry(self, deployed: str, target: str) -> Relation: ...
    def ci_check_runs(self, sha: str) -> Sequence[CheckRun]: ...
    def db_alembic_version(self) -> str | None: ...
    def migration_files(self, rev: str) -> Mapping[str, str]: ...
    def adr_text(self, rev: str, number: str) -> str | None: ...
    def running_workflow_count(self) -> int: ...
    def pending_reservation_count(self) -> int: ...
    def schedule_states(self) -> Mapping[str, Mapping[str, object]]: ...
    def required_queues(self) -> Sequence[str]: ...
    def queues_without_poller(self, queues: Sequence[str]) -> Sequence[str]: ...
    def target_info(self) -> TargetInfo: ...
    def read_text(self, path: str) -> str | None: ...


# ------------------------------------------------------------------ 集約


def aggregate(results: Sequence[CheckResult], required: Sequence[str]) -> Overall:
    """BLOCKED > UNKNOWN > READY。必須が欠けたら UNKNOWN。READY は必須が全て PASS のときだけ。"""
    if any(r.status is Status.BLOCKED for r in results):
        return Overall.BLOCKED
    by_name = {r.name: r.status for r in results}
    if any(r.status is Status.UNKNOWN for r in results):
        return Overall.UNKNOWN
    if any(by_name.get(name) is not Status.PASS for name in required):
        return Overall.UNKNOWN
    return Overall.READY


def exit_code(overall: Overall) -> int:
    return EXIT_CODES[overall]


def default_approval_path(sha: str) -> str:
    return f"release/approvals/{sha}.json"


def default_replay_path(sha: str) -> str:
    return f"release/evidence/{sha}-replay.json"


# ------------------------------------------------------------------ 実行


def _guard(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    """collector の失敗を UNKNOWN にする。想定外の例外も同じ（READY に倒さない）。"""
    try:
        return fn()
    except CollectorUnavailable as exc:
        return CheckResult(name, Status.UNKNOWN, f"unavailable: {exc}")
    except Exception as exc:
        return CheckResult(name, Status.UNKNOWN, f"collector failed: {type(exc).__name__}")


def _unknown(name: str, why: str) -> CheckResult:
    return CheckResult(name, Status.UNKNOWN, why)


def common_revision(revisions: Mapping[str, str | None]) -> str | None:
    """全コンテナが同じ clean な revision のときだけ、その値。"""
    values = set(revisions.values())
    if len(values) != 1:
        return None
    (only,) = values
    return only if only is not None and _SHA.match(only) else None


def run_preflight(req: Request, c: Collectors, now: datetime) -> Report:
    sha_result = _check_target_sha(req.target_sha, c)
    target_box: list[TargetInfo | None] = [None]
    results: list[CheckResult] = [sha_result]
    results.append(_guard(CHECK_TARGET_ENV, lambda: _check_target_env(c, target_box)))
    sha_ok = sha_result.status is Status.PASS

    def dependent(name: str, fn: Callable[[], CheckResult]) -> None:
        results.append(
            _guard(name, fn) if sha_ok else _unknown(name, "skipped: target SHA is not valid")
        )

    if req.verify_post_deploy:
        dependent(CHECK_AT_TARGET, lambda: _check_at_target(req.target_sha, c))
        dependent(CHECK_POLLERS, lambda: _check_pollers(c))
        dependent(CHECK_SCHEDULES_SAME, lambda: _check_schedules_same(req, c))
        dependent(CHECK_ALEMBIC_HEAD, lambda: _check_alembic_head(req.target_sha, c))
        required = POST_DEPLOY_REQUIRED
        mode = "post-deploy"
    else:
        deployed_box: list[str | None] = [None]
        dependent(CHECK_WORKTREE, lambda: _check_worktree(req.target_sha, c))
        dependent(CHECK_CI, lambda: _check_ci(req.target_sha, c))
        dependent(CHECK_CONTAINERS, lambda: _check_containers(c, deployed_box))
        dependent(CHECK_ANCESTRY, lambda: _check_ancestry(req, c, deployed_box[0]))
        dependent(CHECK_ALEMBIC, lambda: _check_alembic(req.target_sha, c))
        dependent(CHECK_MIGRATION, lambda: _check_migration(req.target_sha, c, deployed_box[0]))
        results.append(_guard(CHECK_RUNNING, lambda: _check_running(c)))
        results.append(_guard(CHECK_RESERVATIONS, lambda: _check_reservations(c)))
        results.append(_guard(CHECK_SCHEDULES, lambda: _check_schedule_snapshot(c)))
        results.append(_guard(CHECK_BACKUP, lambda: _check_backup(req, c, now)))
        dependent(CHECK_REPLAY, lambda: _check_replay(req, c))
        dependent(CHECK_APPROVAL, lambda: _check_approval(req, c))
        required = PRE_DEPLOY_REQUIRED
        mode = "pre-deploy"
    if target_box[0] is not None:
        label = f" [{target_label(target_box[0])}]"
        results = [
            replace(r, detail=r.detail + label) if r.name in TARGET_BOUND_CHECKS else r
            for r in results
        ]
    order = {name: i for i, name in enumerate(required)}
    results.sort(key=lambda r: order.get(r.name, len(order)))
    return Report(req.target_sha, mode, required, tuple(results), aggregate(results, required))


# ------------------------------------------------------------------ 個別のチェック


def _check_target_sha(sha: str, c: Collectors) -> CheckResult:
    if not _SHA.match(sha or ""):
        return CheckResult(CHECK_TARGET_SHA, Status.BLOCKED, "target SHA must be a full 40-hex id")

    def exists() -> CheckResult:
        if not c.commit_exists(sha):
            return CheckResult(CHECK_TARGET_SHA, Status.BLOCKED, "commit not found locally")
        return CheckResult(CHECK_TARGET_SHA, Status.PASS, "pinned and present locally")

    return _guard(CHECK_TARGET_SHA, exists)


def target_label(info: TargetInfo) -> str:
    """DB の host / 名前と Temporal namespace だけ（user・password・address は出さない）。"""
    host = info.db_host.rsplit("@", 1)[-1]
    return f"db={host}/{info.db_name} ns={info.temporal_namespace}"


def _check_target_env(c: Collectors, box: list[TargetInfo | None]) -> CheckResult:
    """staging / テスト用 DB を指したまま「本番の判定」をしない。"""
    info = c.target_info()
    box[0] = info
    reasons: list[str] = []
    if info.temporal_namespace == STAGING_TEMPORAL_NAMESPACE:
        reasons.append(f"temporal namespace is {STAGING_TEMPORAL_NAMESPACE!r}")
    if STAGING_ENVIRONMENT_MARK in info.environment.lower():
        reasons.append("environment says staging")
    if info.db_name.endswith(TEST_DB_SUFFIX) or info.db_name == STAGING_DB_NAME:
        reasons.append(f"database {info.db_name!r} is a test/staging database")
    label = target_label(info)
    if reasons:
        return CheckResult(
            CHECK_TARGET_ENV,
            Status.BLOCKED,
            f"not a production target ({'; '.join(reasons)}) [{label}]",
        )
    return CheckResult(CHECK_TARGET_ENV, Status.PASS, f"reads {label}")


def _check_worktree(sha: str, c: Collectors) -> CheckResult:
    state = c.head_state()
    if state.head_sha != sha:
        return CheckResult(
            CHECK_WORKTREE, Status.BLOCKED, f"HEAD is {state.head_sha[:12]}, not the target"
        )
    if state.dirty:
        return CheckResult(CHECK_WORKTREE, Status.BLOCKED, "working tree is dirty")
    return CheckResult(CHECK_WORKTREE, Status.PASS, "clean at the target SHA")


def _check_containers(c: Collectors, deployed_box: list[str | None]) -> CheckResult:
    revisions = c.container_revisions()
    if not revisions:
        return _unknown(CHECK_CONTAINERS, "no avp2 app container found")
    bad = sorted(
        s for s, r in revisions.items() if r is None or r == "unknown" or r.endswith("-dirty")
    )
    if bad:
        return CheckResult(
            CHECK_CONTAINERS, Status.BLOCKED, f"no clean revision label: {', '.join(bad)}"
        )
    kinds = sorted(set(revisions.values()))  # type: ignore[type-var]
    if len(kinds) > 1:
        return CheckResult(
            CHECK_CONTAINERS,
            Status.BLOCKED,
            f"{len(kinds)} different revisions running: {', '.join(k[:12] for k in kinds)}",
        )
    deployed_box[0] = common_revision(revisions)
    return CheckResult(
        CHECK_CONTAINERS,
        Status.PASS,
        f"{len(revisions)} containers at {kinds[0][:12]}",
        {"revision": kinds[0]},
    )


def _check_ancestry(req: Request, c: Collectors, deployed: str | None) -> CheckResult:
    if deployed is None:
        return _unknown(CHECK_ANCESTRY, "deployed revision is not determined")
    relation = c.ancestry(deployed, req.target_sha)
    if relation in (Relation.SAME, Relation.FAST_FORWARD):
        return CheckResult(CHECK_ANCESTRY, Status.PASS, f"{relation.value} from {deployed[:12]}")
    if relation is Relation.UNKNOWN_COMMIT:
        return _unknown(CHECK_ANCESTRY, f"deployed {deployed[:12]} is not in the local repository")
    if req.allow_non_ff:
        return CheckResult(
            CHECK_ANCESTRY, Status.PASS, f"{relation.value} accepted by --allow-non-ff"
        )
    return CheckResult(
        CHECK_ANCESTRY,
        Status.BLOCKED,
        f"target is {relation.value} relative to production {deployed[:12]} (use --allow-non-ff)",
    )


_CI_BAD = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def _check_ci(sha: str, c: Collectors) -> CheckResult:
    """集約 job（``CI_GATE_JOB_NAME``）が success であることだけを READY の根拠にする。

    他の check run が全て success でも、gate が無ければ UNKNOWN（gate は全 job を ``needs`` に
    持つので、gate 抜きの「全部 success」は job を足し忘れた/名前が違うだけかもしれない）。
    """
    gates = [r for r in c.ci_check_runs(sha) if r.name == CI_GATE_JOB_NAME]
    if not gates:
        return _unknown(CHECK_CI, f"no check run named {CI_GATE_JOB_NAME!r} for the SHA")
    if any(r.status == "completed" and r.conclusion in _CI_BAD for r in gates):
        return CheckResult(CHECK_CI, Status.BLOCKED, f"{CI_GATE_JOB_NAME!r} did not succeed")
    if any(r.status != "completed" or r.conclusion != "success" for r in gates):
        return _unknown(CHECK_CI, f"{CI_GATE_JOB_NAME!r} not finished or not a success")
    return CheckResult(CHECK_CI, Status.PASS, f"{CI_GATE_JOB_NAME!r} succeeded")


# ---- alembic / migration


_REV = re.compile(r'^revision\s*(?::[^=]+)?=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DOWN = re.compile(r'^down_revision\s*(?::[^=]+)?=\s*(None|["\']([^"\']+)["\'])', re.MULTILINE)


def alembic_graph(files: Mapping[str, str]) -> dict[str, str | None]:
    graph: dict[str, str | None] = {}
    for text in files.values():
        rev, down = _REV.search(text), _DOWN.search(text)
        if rev and down:
            graph[rev.group(1)] = down.group(2)
    return graph


def alembic_head(files: Mapping[str, str]) -> str | None:
    graph = alembic_graph(files)
    heads = set(graph) - {d for d in graph.values() if d}
    return next(iter(heads)) if len(heads) == 1 else None


def _chain(graph: Mapping[str, str | None], head: str) -> set[str]:
    seen: set[str] = set()
    cur: str | None = head
    while cur is not None and cur in graph and cur not in seen:
        seen.add(cur)
        cur = graph[cur]
    return seen


def _check_alembic(sha: str, c: Collectors) -> CheckResult:
    prod = c.db_alembic_version()
    if not prod:
        return _unknown(CHECK_ALEMBIC, "alembic_version is not readable")
    files = c.migration_files(sha)
    head = alembic_head(files)
    if head is None:
        return _unknown(CHECK_ALEMBIC, "target alembic head is not determined")
    if prod not in _chain(alembic_graph(files), head):
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            f"production {prod} is not on the target chain (head {head})",
        )
    pending = prod != head
    return CheckResult(
        CHECK_ALEMBIC,
        Status.PASS,
        f"production {prod}, target head {head}" + (" (migrations will run)" if pending else ""),
        {"production": prod, "head": head},
    )


_MARKER = re.compile(r"^#\s*contract-migration:\s*approved\s+ADR-(\d{4})\b")
_DESTRUCTIVE_CALLS = {"rename_table"}
#: 呼び出し名が drop で始まるものは全て破壊的（drop_column / drop_table / drop_index /
#: drop_constraint / Enum.drop ...）
_SQL_DESTRUCTIVE = re.compile(
    r"\bDROP\b|\bRENAME\b|\bTRUNCATE\b|\bDELETE\s+FROM\b|SET\s+NOT\s+NULL"
    r"|ALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b",
    re.IGNORECASE,
)
_SQL_ADD_NOT_NULL = re.compile(r"\bADD\b[^;]*\bNOT\s+NULL\b", re.IGNORECASE)
_SQL_DEFAULT = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_OPAQUE_CALLS = {"eval", "exec", "__import__", "getattr"}
_EXECUTE_CALLS = {"execute", "exec_driver_sql"}


@dataclass(frozen=True)
class Finding:
    """kind: ``destructive``（expand-only でない）/ ``unscannable``（静的に判定できない）。"""

    kind: str
    text: str


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return getattr(node.func, "id", "")


def _sql_findings(value: str, line: int) -> list[Finding]:
    out: list[Finding] = []
    if _SQL_DESTRUCTIVE.search(value):
        out.append(Finding("destructive", f"destructive SQL (line {line})"))
    for stmt in value.split(";"):
        if _SQL_ADD_NOT_NULL.search(stmt) and not _SQL_DEFAULT.search(stmt):
            out.append(Finding("destructive", f"ADD ... NOT NULL without DEFAULT (line {line})"))
    return out


def _literal_sql(arg: ast.expr) -> str | None:
    """``"..."`` または ``text("...")`` だけが静的に読める。"""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if (
        isinstance(arg, ast.Call)
        and _call_name(arg) == "text"
        and arg.args
        and isinstance(arg.args[0], ast.Constant)
        and isinstance(arg.args[0].value, str)
    ):
        return arg.args[0].value
    return None


def _nullability_findings(call: ast.Call, what: str) -> list[Finding]:
    """NOT NULL を server_default（値が None でないもの）無しで足していないか。"""
    if any(kw.arg is None for kw in call.keywords):
        return [Finding("unscannable", f"{what}: **kwargs (line {call.lineno})")]
    kws = {kw.arg: kw.value for kw in call.keywords}
    not_null = False
    out: list[Finding] = []
    for key, flag in (("nullable", False), ("primary_key", True)):
        if key not in kws:
            continue
        value = kws[key]
        if not isinstance(value, ast.Constant):
            out.append(
                Finding("unscannable", f"{what}: {key} is not a literal (line {call.lineno})")
            )
        elif value.value is flag:
            not_null = True
    default = kws.get("server_default")
    has_default = default is not None and not (
        isinstance(default, ast.Constant) and default.value is None
    )
    if not_null and not has_default:
        out.append(
            Finding("destructive", f"{what}: NOT NULL without server_default (line {call.lineno})")
        )
    return out


def _call_findings(node: ast.Call) -> list[Finding]:
    name = _call_name(node)
    line = node.lineno
    if name.startswith("drop") or name in _DESTRUCTIVE_CALLS:
        return [Finding("destructive", f"{name} (line {line})")]
    if name in _OPAQUE_CALLS:
        return [Finding("unscannable", f"{name}() (line {line})")]
    if name == "alter_column":
        keys = {kw.arg for kw in node.keywords}
        if "new_column_name" in keys:
            return [Finding("destructive", f"rename column (line {line})")]
        if "type_" in keys:
            return [Finding("destructive", f"column type change (line {line})")]
        return _nullability_findings(node, "alter_column")
    if name == "add_column":
        column = node.args[1] if len(node.args) > 1 else None
        if column is None:
            column = next((kw.value for kw in node.keywords if kw.arg == "column"), None)
        if not isinstance(column, ast.Call):
            return [Finding("unscannable", f"add_column with a non-literal column (line {line})")]
        return _nullability_findings(column, "add_column")
    if name in _EXECUTE_CALLS and (not node.args or _literal_sql(node.args[0]) is None):
        return [Finding("unscannable", f"{name} with a non-literal statement (line {line})")]
    return []


def scan_migration(source: str) -> list[Finding]:
    """``upgrade()`` と、そこから（同じファイル内で）呼ばれる関数だけを見る。

    downgrade の drop は expand migration でも普通にあるので見ない。**best effort**:
    静的な走査は抜けうる。通過は「安全の証明」ではなく、人のレビューは省けない。
    """
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "upgrade" not in funcs:
        return [Finding("unscannable", "no upgrade() found")]
    reachable: list[ast.FunctionDef] = []
    seen: set[str] = set()
    queue = ["upgrade"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        reachable.append(funcs[name])
        for node in ast.walk(funcs[name]):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in funcs
            ):
                queue.append(node.func.id)
    findings: list[Finding] = []
    for fn in reachable:
        docstring: ast.AST | None = None
        first = fn.body[0] if fn.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docstring = first.value
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                findings += _call_findings(node)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node is not docstring
            ):
                findings += _sql_findings(node.value, node.lineno)
    return findings


def contract_marker_numbers(source: str) -> list[str]:
    """``# contract-migration: approved ADR-NNNN`` を **コメント token** からだけ拾う。

    文字列・docstring の中に書かれた同じ文面は数えない。
    """
    numbers: list[str] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            match = _MARKER.match(tok.string)
            if match:
                numbers.append(match.group(1))
    return numbers


_ADR_STATUS = re.compile(r"^##\s+Status\s*\n+\s*([^\n]+)", re.MULTILINE)


def adr_approves_migration(adr: str | None, revision: str | None) -> bool:
    """ADR が ``## Status`` Accepted で、その migration の revision id に言及している。"""
    if adr is None or revision is None:
        return False
    status = _ADR_STATUS.search(adr)
    if status is None or not status.group(1).strip().startswith("Accepted"):
        return False
    return re.search(rf"(?<!\w){re.escape(revision)}(?!\w)", adr) is not None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _check_migration(sha: str, c: Collectors, deployed: str | None) -> CheckResult:
    if deployed is None:
        return _unknown(CHECK_MIGRATION, "deployed revision is not determined")
    base = c.migration_files(deployed)
    target = c.migration_files(sha)
    # 本番に入った migration は不変。書き換え・削除は、過去の revision に破壊的変更を忍ばせる
    edited = sorted(n for n in base if n in target and _sha256(base[n]) != _sha256(target[n]))
    removed = sorted(n for n in base if n not in target)
    if edited or removed:
        parts = [f"edited in place: {', '.join(edited)}"] if edited else []
        parts += [f"removed: {', '.join(removed)}"] if removed else []
        return CheckResult(
            CHECK_MIGRATION,
            Status.BLOCKED,
            "deployed migrations changed (" + "; ".join(parts) + ")",
        )
    new = {name: text for name, text in target.items() if name not in base}
    if not new:
        return CheckResult(CHECK_MIGRATION, Status.PASS, "no new migration")
    blocked: list[str] = []
    unknown: list[str] = []
    for name, text in sorted(new.items()):
        try:
            found = scan_migration(text)
            markers = contract_marker_numbers(text)
        except (SyntaxError, tokenize.TokenError):
            return _unknown(CHECK_MIGRATION, f"{name} cannot be parsed")
        if not found:
            continue
        rev = _REV.search(text)
        revision = rev.group(1) if rev else None
        if any(adr_approves_migration(c.adr_text(deployed, n), revision) for n in markers):
            continue
        destructive = [f.text for f in found if f.kind == "destructive"]
        if destructive:
            blocked.append(f"{name}: {'; '.join(destructive)}")
        else:
            unknown.append(f"{name}: {'; '.join(f.text for f in found)}")
    if blocked:
        return CheckResult(
            CHECK_MIGRATION,
            Status.BLOCKED,
            "contract migration without a valid approved-ADR marker "
            "(ADR deployed, Accepted, names the revision): " + " | ".join(blocked),
        )
    if unknown:
        return _unknown(CHECK_MIGRATION, "cannot be judged statically: " + " | ".join(unknown))
    return CheckResult(
        CHECK_MIGRATION,
        Status.PASS,
        f"{len(new)} new migration(s), no contract change found "
        "(best effort; human review required)",
    )


# ---- 実行中・予約・Schedule


def _check_running(c: Collectors) -> CheckResult:
    n = c.running_workflow_count()
    return CheckResult(
        CHECK_RUNNING, Status.PASS, f"{n} running workflow(s) (recorded)", {"count": n}
    )


def _check_reservations(c: Collectors) -> CheckResult:
    n = c.pending_reservation_count()
    if n > 0:
        return CheckResult(
            CHECK_RESERVATIONS,
            Status.BLOCKED,
            f"{n} unreconciled provider reservation(s) (resend risk, ADR-0013)",
            {"count": n},
        )
    return CheckResult(CHECK_RESERVATIONS, Status.PASS, "no pending reservation", {"count": 0})


def _check_schedule_snapshot(c: Collectors) -> CheckResult:
    states = {k: dict(v) for k, v in c.schedule_states().items()}
    if not states:
        return _unknown(CHECK_SCHEDULES, "no schedule state readable")
    summary = ", ".join(f"{k}: paused={v.get('paused')}" for k, v in sorted(states.items()))
    return CheckResult(CHECK_SCHEDULES, Status.PASS, summary, {"schedules": states})


def schedule_snapshot_from_report(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        for r in json.loads(text)["results"]:
            if r["name"] == CHECK_SCHEDULES and r["status"] == Status.PASS:
                return dict(r["data"]["schedules"])
    except (ValueError, KeyError, TypeError):
        return None
    return None


# ---- 証跡ファイル


def _load_json(text: str | None) -> dict[str, Any] | None:
    if text is None:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _check_backup(req: Request, c: Collectors, now: datetime) -> CheckResult:
    if req.backup_manifest is None:
        return _unknown(CHECK_BACKUP, "no backup manifest given (--backup-manifest)")
    manifest = _load_json(c.read_text(req.backup_manifest))
    if manifest is None:
        return _unknown(CHECK_BACKUP, "backup manifest absent or not JSON")
    comps = manifest.get("components")
    comps = comps if isinstance(comps, dict) else {}
    limit = timedelta(hours=req.backup_max_age_hours)
    stale: list[str] = []
    missing: list[str] = []
    for name in BACKUP_COMPONENTS:
        entry = comps.get(name)
        at = _parse_time(entry.get("created_at")) if isinstance(entry, dict) else None
        if at is None or at.tzinfo is None:
            missing.append(name)
        elif now - at > limit or at - now > timedelta(minutes=5):
            stale.append(name)
    if stale:
        return CheckResult(
            CHECK_BACKUP,
            Status.BLOCKED,
            f"stale or future-dated (> {req.backup_max_age_hours:g}h): {', '.join(stale)}",
        )
    if missing:
        return _unknown(
            CHECK_BACKUP, f"components not confirmed in the manifest: {', '.join(missing)}"
        )
    return CheckResult(
        CHECK_BACKUP, Status.PASS, f"all components within {req.backup_max_age_hours:g}h"
    )


def _check_replay(req: Request, c: Collectors) -> CheckResult:
    path = req.replay_evidence or default_replay_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_REPLAY, f"replay evidence absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(CHECK_REPLAY, Status.BLOCKED, "replay evidence is for a different SHA")
    if doc.get("result") == "pass":
        return CheckResult(CHECK_REPLAY, Status.PASS, "replay passed for the SHA")
    if doc.get("result") == "fail":
        return CheckResult(CHECK_REPLAY, Status.BLOCKED, "replay failed")
    return _unknown(CHECK_REPLAY, "replay evidence has no result")


def _check_approval(req: Request, c: Collectors) -> CheckResult:
    path = req.approval_file or default_approval_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_APPROVAL, f"approval record absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(CHECK_APPROVAL, Status.BLOCKED, "approval is for a different SHA")
    if not doc.get("approver") or _parse_time(doc.get("approved_at")) is None:
        return _unknown(CHECK_APPROVAL, "approval record lacks approver or approved_at")
    return CheckResult(CHECK_APPROVAL, Status.PASS, f"approved by {doc['approver']}")


# ---- deploy 後


def _check_at_target(sha: str, c: Collectors) -> CheckResult:
    revisions = c.container_revisions()
    if not revisions:
        return _unknown(CHECK_AT_TARGET, "no avp2 app container found")
    wrong = sorted(s for s, r in revisions.items() if r != sha)
    if wrong:
        return CheckResult(CHECK_AT_TARGET, Status.BLOCKED, f"not at target: {', '.join(wrong)}")
    return CheckResult(CHECK_AT_TARGET, Status.PASS, f"{len(revisions)} containers at the target")


def _check_pollers(c: Collectors) -> CheckResult:
    queues = list(c.required_queues())
    if not queues:
        return _unknown(CHECK_POLLERS, "required queue list is empty")
    missing = list(c.queues_without_poller(queues))
    if missing:
        return CheckResult(CHECK_POLLERS, Status.BLOCKED, f"no poller: {', '.join(missing)}")
    return CheckResult(CHECK_POLLERS, Status.PASS, f"pollers present on {len(queues)} queues")


def _check_schedules_same(req: Request, c: Collectors) -> CheckResult:
    before = schedule_snapshot_from_report(c.read_text(req.pre_report) if req.pre_report else None)
    if before is None:
        return _unknown(CHECK_SCHEDULES_SAME, "no pre-deploy schedule snapshot (--pre-report)")
    after = {k: dict(v) for k, v in c.schedule_states().items()}
    if after != before:
        diff = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
        return CheckResult(
            CHECK_SCHEDULES_SAME,
            Status.BLOCKED,
            f"differs from the pre-deploy snapshot: {', '.join(diff)}",
        )
    return CheckResult(CHECK_SCHEDULES_SAME, Status.PASS, "equal to the pre-deploy snapshot")


def _check_alembic_head(sha: str, c: Collectors) -> CheckResult:
    prod = c.db_alembic_version()
    head = alembic_head(c.migration_files(sha))
    if not prod or head is None:
        return _unknown(CHECK_ALEMBIC_HEAD, "alembic_version or target head is not determined")
    if prod != head:
        return CheckResult(CHECK_ALEMBIC_HEAD, Status.BLOCKED, f"production {prod} != head {head}")
    return CheckResult(CHECK_ALEMBIC_HEAD, Status.PASS, f"at head {head}")


# ------------------------------------------------------------------ 出力


def report_json(report: Report) -> str:
    return json.dumps(
        {
            "target_sha": report.target_sha,
            "mode": report.mode,
            "overall": report.overall.value,
            "required": list(report.required),
            "results": [
                {"name": r.name, "status": r.status.value, "detail": r.detail, "data": dict(r.data)}
                for r in report.results
            ],
        },
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def render_text(report: Report) -> str:
    lines = [f"release preflight ({report.mode}) target={report.target_sha or '-'}"]
    have = {r.name for r in report.results}
    for r in report.results:
        lines.append(f"[{r.status.value:^7}] {r.name}: {r.detail}")
    for name in report.required:
        if name not in have:
            lines.append(f"[UNKNOWN] {name}: not evaluated")
    lines.append("")
    lines.append(f"OVERALL: {report.overall.value}")
    return "\n".join(lines)
