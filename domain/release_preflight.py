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
from domain.backup.manifest import drill_problems

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
MAX_BACKUP_MAX_AGE_HOURS = 720.0
#: 承認・replay 証跡の最大の古さ。設定できるが上限で縛る（古い承認の流用を防ぐ）
DEFAULT_EVIDENCE_MAX_AGE_HOURS = 72.0
MAX_EVIDENCE_MAX_AGE_HOURS = 168.0
#: 時計のずれとして許す未来の幅
CLOCK_SKEW = timedelta(minutes=5)

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
CHECK_SERVICE_HEALTH = "service_health"
CHECK_REPLAY = "replay_evidence"
CHECK_APPROVAL = "owner_approval"

CHECK_AT_TARGET = "containers_at_target"
CHECK_POLLERS = "worker_pollers"
CHECK_SCHEDULES_SAME = "schedules_unchanged"
CHECK_ALEMBIC_HEAD = "alembic_at_head"
CHECK_APPROVAL_BINDING = "approval_binding"

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
    CHECK_SERVICE_HEALTH,
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
    CHECK_APPROVAL_BINDING,
)

#: 本番の DB / Temporal を読んで判定するチェック。対象の取り違えに気づけるよう detail に宛先を出す
TARGET_BOUND_CHECKS = frozenset(
    {
        "alembic",
        "running_workflows",
        "provider_reservations",
        "service_health",
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
    #: owner が承認した preflight の ``--json`` 出力。承認の ``report_sha256`` と照合する
    approved_report: str | None = None
    evidence_max_age_hours: float = DEFAULT_EVIDENCE_MAX_AGE_HOURS
    #: 前の SHA へ戻す preflight / 事後検証（DB が target より先に進んでいることを明示的に判定する）
    rollback: bool = False


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
    def unhealthy_services(self) -> Sequence[str]: ...
    def schedule_states(self) -> Mapping[str, Mapping[str, object]]: ...
    def required_queues(self) -> Sequence[str]: ...
    def queues_without_poller(self, queues: Sequence[str]) -> Sequence[str]: ...
    def expected_services(self) -> Sequence[str]: ...
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


def report_mode(req: Request) -> str:
    """出力の ``mode``。pre-report / approved-report の取り違え検査（#11）はこの値で照合する。"""
    base = "post-deploy" if req.verify_post_deploy else "pre-deploy"
    return f"rollback-{base}" if req.rollback else base


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
        dependent(CHECK_ALEMBIC_HEAD, lambda: _check_alembic_head(req, c))
        dependent(CHECK_APPROVAL_BINDING, lambda: _check_approval_binding(req, c))
        required = POST_DEPLOY_REQUIRED
    else:
        deployed_box: list[str | None] = [None]
        dependent(CHECK_WORKTREE, lambda: _check_worktree(req.target_sha, c))
        dependent(CHECK_CI, lambda: _check_ci(req.target_sha, c))
        dependent(CHECK_CONTAINERS, lambda: _check_containers(c, deployed_box))
        dependent(CHECK_ANCESTRY, lambda: _check_ancestry(req, c, deployed_box[0]))
        dependent(CHECK_ALEMBIC, lambda: _check_alembic(req, c, deployed_box[0]))
        dependent(CHECK_MIGRATION, lambda: _check_migration(req, c, deployed_box[0]))
        results.append(_guard(CHECK_RUNNING, lambda: _check_running(c)))
        results.append(_guard(CHECK_RESERVATIONS, lambda: _check_reservations(c)))
        results.append(_guard(CHECK_SCHEDULES, lambda: _check_schedule_snapshot(c)))
        results.append(_guard(CHECK_SERVICE_HEALTH, lambda: _check_service_health(c)))
        results.append(_guard(CHECK_BACKUP, lambda: _check_backup(req, c, now)))
        dependent(CHECK_REPLAY, lambda: _check_replay(req, c, now))
        dependent(CHECK_APPROVAL, lambda: _check_approval(req, c, now))
        required = PRE_DEPLOY_REQUIRED
    if target_box[0] is not None:
        label = f" [{target_label(target_box[0])}]"
        results = [
            replace(r, detail=r.detail + label) if r.name in TARGET_BOUND_CHECKS else r
            for r in results
        ]
    order = {name: i for i, name in enumerate(required)}
    results.sort(key=lambda r: order.get(r.name, len(order)))
    return Report(
        req.target_sha, report_mode(req), required, tuple(results), aggregate(results, required)
    )


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


def _missing_services(
    name: str, c: Collectors, revisions: Mapping[str, str | None]
) -> CheckResult | None:
    """常駐しているべきアプリサービス（deploy-workers.sh の APP_SERVICES 由来）が欠けていないか。"""
    expected = list(c.expected_services())
    if not expected:
        return _unknown(name, "expected app service list is empty")
    missing = sorted(set(expected) - set(revisions))
    if missing:
        return CheckResult(name, Status.BLOCKED, f"app service not running: {', '.join(missing)}")
    return None


def _check_containers(c: Collectors, deployed_box: list[str | None]) -> CheckResult:
    revisions = c.container_revisions()
    if not revisions:
        return _unknown(CHECK_CONTAINERS, "no avp2 app container found")
    absent = _missing_services(CHECK_CONTAINERS, c, revisions)
    if absent is not None:
        return absent
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
    if req.rollback and relation is Relation.BEHIND:
        return CheckResult(
            CHECK_ANCESTRY, Status.PASS, f"rollback to an ancestor of production {deployed[:12]}"
        )
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


def _revision_texts(files: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for text in files.values():
        rev = _REV.search(text)
        if rev:
            out[rev.group(1)] = text
    return out


def _check_alembic(req: Request, c: Collectors, deployed: str | None) -> CheckResult:
    prod = c.db_alembic_version()
    if not prod:
        return _unknown(CHECK_ALEMBIC, "alembic_version is not readable")
    files = c.migration_files(req.target_sha)
    head = alembic_head(files)
    if head is None:
        return _unknown(CHECK_ALEMBIC, "target alembic head is not determined")
    if prod not in _chain(alembic_graph(files), head):
        if req.rollback:
            return _check_db_ahead(req, c, deployed, prod, head)
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            f"production {prod} is not on the target chain (head {head}); "
            "if this is a rollback use --rollback",
        )
    pending = prod != head
    return CheckResult(
        CHECK_ALEMBIC,
        Status.PASS,
        f"production {prod}, target head {head}" + (" (migrations will run)" if pending else ""),
        {"production": prod, "head": head},
    )


def _check_db_ahead(
    req: Request, c: Collectors, deployed: str | None, prod: str, head: str
) -> CheckResult:
    """rollback: DB が target より先。expand-only かつ owner が旧コード互換と承認したときだけ PASS。

    旧イメージの ``alembic upgrade head`` は DB が先にいると失敗する
    （``Can't locate revision``）ので、この PASS は deploy-workers.sh を ``SKIP_MIGRATE=1`` で
    走らせることが前提（release-runbook §4）。
    """
    if deployed is None:
        return _unknown(CHECK_ALEMBIC, "deployed revision is not determined")
    running = c.migration_files(deployed)
    graph = alembic_graph(running)
    if prod not in graph:
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            f"production {prod} is unknown to the running code ({deployed[:12]}) as well",
        )
    steps: list[str] = []
    cur: str | None = prod
    while cur is not None and cur != head and cur in graph and cur not in steps:
        steps.append(cur)
        cur = graph[cur]
    if cur != head:
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            f"target head {head} is not an ancestor of production {prod} (diverged history)",
        )
    texts = _revision_texts(running)
    contract: list[str] = []
    for rev in steps:
        text = texts.get(rev)
        try:
            found = (
                scan_migration(text) if text is not None else [Finding("unscannable", "no file")]
            )
        except SyntaxError:
            return _unknown(CHECK_ALEMBIC, f"migration {rev} cannot be parsed")
        if found:
            contract.append(f"{rev}: {'; '.join(f.text for f in found)}")
    if contract:
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            "migrations ahead of the target are not expand-only (release-runbook §5): "
            + " | ".join(contract),
        )
    ahead = sorted(steps)
    path = req.approval_file or default_approval_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_ALEMBIC, f"approval record absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(CHECK_ALEMBIC, Status.BLOCKED, "approval is for a different SHA")
    flag = doc.get("rollback")
    if not isinstance(flag, dict) or "old_code_compatible" not in flag:
        return _unknown(
            CHECK_ALEMBIC,
            f"DB is ahead by {', '.join(ahead)}; the owner has not recorded "
            "rollback.old_code_compatible in the approval file",
        )
    if flag.get("old_code_compatible") is not True or sorted(flag.get("revisions") or []) != ahead:
        return CheckResult(
            CHECK_ALEMBIC,
            Status.BLOCKED,
            f"approval does not accept old-code compatibility for exactly {', '.join(ahead)}",
        )
    return CheckResult(
        CHECK_ALEMBIC,
        Status.PASS,
        f"rollback: production {prod} is {len(ahead)} expand-only migration(s) ahead of target "
        f"head {head} ({', '.join(ahead)}), accepted by the owner; deploy with SKIP_MIGRATE=1",
        {"production": prod, "head": head, "rollback_ahead": ahead},
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


_WIDEN_NAME = "CHECK_WIDENING"
_WIDEN_KEYS = frozenset({"table", "constraint", "column", "old", "new"})
_IDENT = re.compile(r"[a-z_][a-z0-9_]*")
#: 宣言できる制約名は CHECK の命名規約（``ck_`` 始まり）だけ。PK/FK/UNIQUE（``*_pkey`` /
#: ``*_fkey`` / ``*_key`` / ``uq_*`` / ``fk_*`` / ``pk_*``）の名前を CHECK と偽って
#: 宣言しても免除しない（静的走査は制約の種類を DB から引けないので、名前の規約で絞る）
_CHECK_NAME = re.compile(r"ck_[a-z0-9_]+")
_NOT_CHECK_SUFFIX = ("_pkey", "_fkey", "_key", "_pk", "_fk", "_uq", "_unique")
_VALUE = re.compile(r"[A-Z][A-Z0-9_]*")
_IN_LIST = re.compile(
    r"(?P<col>[a-z_][a-z0-9_]*) IN \((?P<vals>'[A-Z][A-Z0-9_]*'(?:, '[A-Z][A-Z0-9_]*')*)\)"
)


@dataclass(frozen=True)
class _Widening:
    table: str
    constraint: str
    column: str
    old: tuple[str, ...]
    new: tuple[str, ...]

    def describe(self) -> str:
        return (
            f"{self.constraint} on {self.table}: {len(self.old)} -> {len(self.new)} allowed values"
        )


def _widening_decl(tree: ast.Module) -> tuple[_Widening | None, list[Finding]]:
    """モジュール直下の ``CHECK_WIDENING = {...}``（リテラルのみ）を読む。

    宣言が無ければ (None, [])。宣言が不正（キー過不足・非リテラル・重複・旧集合が新集合の
    真の部分集合でない）なら destructive の Finding を返し、拡張としては扱わない。
    関数内・文字列・コメントの同名は無視する。
    """
    targets = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == _WIDEN_NAME for t in n.targets)
    ]
    if not targets:
        return None, []

    def bad(why: str) -> tuple[None, list[Finding]]:
        return None, [Finding("destructive", f"{_WIDEN_NAME} declaration invalid: {why}")]

    if len(targets) != 1:
        return bad("declared more than once")
    try:
        value = ast.literal_eval(targets[0].value)
    except ValueError:
        return bad("not a literal")
    if not isinstance(value, dict) or set(value) != _WIDEN_KEYS:
        return bad(f"keys must be exactly {sorted(_WIDEN_KEYS)}")
    for key in ("table", "constraint", "column"):
        if not isinstance(value[key], str) or not _IDENT.fullmatch(value[key]):
            return bad(f"{key} must be a lowercase identifier literal")
    constraint = value["constraint"]
    if not _CHECK_NAME.fullmatch(constraint) or constraint.endswith(_NOT_CHECK_SUFFIX):
        return bad("constraint must be a CHECK name (ck_*), never a PK/FK/UNIQUE name")
    sets: dict[str, tuple[str, ...]] = {}
    for key in ("old", "new"):
        v = value[key]
        if (
            not isinstance(v, tuple)
            or not v
            or not all(isinstance(x, str) and _VALUE.fullmatch(x) for x in v)
        ):
            return bad(f"{key} must be a non-empty tuple of UPPER_CASE string literals")
        if len(set(v)) != len(v):
            return bad(f"{key} has duplicates")
        sets[key] = v
    if not set(sets["old"]) < set(sets["new"]):
        return bad("old must be a strict subset of new (every old value stays allowed)")
    spec = _Widening(value["table"], value["constraint"], value["column"], sets["old"], sets["new"])
    return spec, []


def _str_const(node: ast.expr | None, expected: str | None = None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value if expected is None or node.value == expected else None
    return None


def _method_call(stmt: ast.stmt, var: str, attr: str) -> ast.Call | None:
    """``var.attr(...)`` だけの文なら、その Call。"""
    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call)):
        return None
    func = stmt.value.func
    if not (
        isinstance(func, ast.Attribute)
        and func.attr == attr
        and isinstance(func.value, ast.Name)
        and func.value.id == var
    ):
        return None
    return stmt.value


def _widening_nodes(fn: ast.FunctionDef, spec: _Widening) -> set[int] | None:
    """``with op.batch_alter_table(T) as b:`` が drop → create の 2 文だけのときの node id。

    drop は ``b.drop_constraint(C, type_="check")``、
    create は ``b.create_check_constraint(C, "col IN (...)")``。
    C・T・col は宣言と一致し、値の集合は宣言の ``new`` と一致すること。
    ``batch_alter_table`` にキーワード引数があれば免除しない。
    """
    for node in ast.walk(fn):
        if not isinstance(node, ast.With) or len(node.items) != 1 or len(node.body) != 2:
            continue
        item = node.items[0]
        ctx = item.context_expr
        if not (
            isinstance(ctx, ast.Call)
            and isinstance(ctx.func, ast.Attribute)
            and ctx.func.attr == "batch_alter_table"
            and isinstance(ctx.func.value, ast.Name)
            and ctx.func.value.id == "op"
            and len(ctx.args) == 1
            # recreate= / copy_from= / table_args= / reflect_args= / schema= などは表を作り直したり
            # 宣言外の対象へ向けたりできる。1 つでもあれば免除しない
            and not ctx.keywords
            and _str_const(ctx.args[0], spec.table) is not None
            and isinstance(item.optional_vars, ast.Name)
        ):
            continue
        var = item.optional_vars.id
        drop = _method_call(node.body[0], var, "drop_constraint")
        create = _method_call(node.body[1], var, "create_check_constraint")
        if drop is None or create is None:
            continue
        if not (
            len(drop.args) == 1
            and _str_const(drop.args[0], spec.constraint) is not None
            and len(drop.keywords) == 1
            and drop.keywords[0].arg == "type_"
            and _str_const(drop.keywords[0].value, "check") is not None
            and len(create.args) == 2
            and not create.keywords
            and _str_const(create.args[0], spec.constraint) is not None
        ):
            continue
        cond = _str_const(create.args[1])
        match = _IN_LIST.fullmatch(cond) if cond is not None else None
        if match is None or match.group("col") != spec.column:
            continue
        values = [v.strip("'") for v in match.group("vals").split(", ")]
        if len(set(values)) != len(values) or set(values) != set(spec.new):
            continue
        return {id(drop), id(create), id(create.args[1])}
    return None


def _scan(source: str) -> tuple[list[Finding], list[str]]:
    tree = ast.parse(source)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if "upgrade" not in funcs:
        return [Finding("unscannable", "no upgrade() found")], []
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
    spec, findings = _widening_decl(tree)
    # 宣言どおりの CHECK 拡張は **1 件だけ** 免除する（ADR-0060）。それ以外の drop は従来どおり
    exempt: set[int] = set()
    widenings: list[str] = []
    if spec is not None:
        for fn in reachable:
            nodes = _widening_nodes(fn, spec)
            if nodes is not None:
                exempt = nodes
                widenings.append(spec.describe())
                break
    for fn in reachable:
        docstring: ast.AST | None = None
        first = fn.body[0] if fn.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            docstring = first.value
        for node in ast.walk(fn):
            if id(node) in exempt:
                continue
            if isinstance(node, ast.Call):
                findings += _call_findings(node)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node is not docstring
            ):
                findings += _sql_findings(node.value, node.lineno)
    return findings, widenings


def scan_migration(source: str) -> list[Finding]:
    """``upgrade()`` と、そこから（同じファイル内で）呼ばれる関数だけを見る。

    downgrade の drop は expand migration でも普通にあるので見ない。**best effort**:
    静的な走査は抜けうる。通過は「安全の証明」ではなく、人のレビューは省けない。
    CHECK 語彙の拡張だけは、宣言（``CHECK_WIDENING``）と実際の 2 文が一致するときに限り
    contract として数えない（ADR-0060）。
    """
    return _scan(source)[0]


def check_widenings(source: str) -> list[str]:
    """走査が「CHECK 語彙の拡張」として免除した宣言の説明（preflight の出力に残すため）。"""
    return _scan(source)[1]


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


def _check_migration(req: Request, c: Collectors, deployed: str | None) -> CheckResult:
    sha = req.target_sha
    if deployed is None:
        return _unknown(CHECK_MIGRATION, "deployed revision is not determined")
    base = c.migration_files(deployed)
    target = c.migration_files(sha)
    # 本番に入った migration は不変。書き換え・削除は、過去の revision に破壊的変更を忍ばせる
    edited = sorted(n for n in base if n in target and _sha256(base[n]) != _sha256(target[n]))
    # rollback では、target が知らない（先に進んだ）migration が消えて見えるのが正しい。
    # それは alembic チェック（_check_db_ahead）が expand-only かどうかを判定する
    removed = [] if req.rollback else sorted(n for n in base if n not in target)
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
    widened: list[str] = []
    for name, text in sorted(new.items()):
        try:
            found = scan_migration(text)
            markers = contract_marker_numbers(text)
            widened += [f"{name}: {w}" for w in check_widenings(text)]
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
    note = (
        f"; CHECK widening (ADR-0060, old values stay allowed): {' | '.join(widened)}"
        if widened
        else ""
    )
    return CheckResult(
        CHECK_MIGRATION,
        Status.PASS,
        f"{len(new)} new migration(s), no contract change found "
        f"(best effort; human review required){note}",
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


def _check_service_health(c: Collectors) -> CheckResult:
    sick = sorted(c.unhealthy_services())
    if sick:
        return CheckResult(
            CHECK_SERVICE_HEALTH,
            Status.BLOCKED,
            f"unhealthy service(s): {', '.join(sick)} (health is never loosened to pass)",
            {"unhealthy": sick},
        )
    return CheckResult(CHECK_SERVICE_HEALTH, Status.PASS, "no unhealthy service")


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


def _bounded(hours: object, upper: float) -> bool:
    return isinstance(hours, int | float) and 0 < hours <= upper  # NaN は比較が偽になり弾かれる


def _staleness(at: datetime, now: datetime, max_hours: float) -> str | None:
    """``at`` が未来、または古すぎれば理由。承認・証跡は自己申告なので日付で縛る。"""
    if at - now > CLOCK_SKEW:
        return "dated in the future"
    if now - at > timedelta(hours=max_hours):
        return f"older than {max_hours:g}h"
    return None


def _check_backup(req: Request, c: Collectors, now: datetime) -> CheckResult:
    if req.backup_manifest is None:
        return _unknown(CHECK_BACKUP, "no backup manifest given (--backup-manifest)")
    if not _bounded(req.backup_max_age_hours, MAX_BACKUP_MAX_AGE_HOURS):
        return _unknown(
            CHECK_BACKUP, f"--backup-max-age-hours must be in (0, {MAX_BACKUP_MAX_AGE_HOURS:g}]"
        )
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
    if manifest.get("restore_verified") is not True:
        return _unknown(
            CHECK_BACKUP, "backup is not restore-verified (no passed restore drill; INV-66)"
        )
    forged = drill_problems(manifest.get("restore_drill"))
    if forged:
        return _unknown(CHECK_BACKUP, f"restore drill evidence not credible: {forged[0]}")
    return CheckResult(
        CHECK_BACKUP,
        Status.PASS,
        f"all components within {req.backup_max_age_hours:g}h, restore-verified",
    )


def _check_replay(req: Request, c: Collectors, now: datetime) -> CheckResult:
    path = req.replay_evidence or default_replay_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_REPLAY, f"replay evidence absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(CHECK_REPLAY, Status.BLOCKED, "replay evidence is for a different SHA")
    if not _bounded(req.evidence_max_age_hours, MAX_EVIDENCE_MAX_AGE_HOURS):
        return _unknown(
            CHECK_REPLAY, f"--evidence-max-age-hours must be in (0, {MAX_EVIDENCE_MAX_AGE_HOURS:g}]"
        )
    if doc.get("result") == "fail":
        return CheckResult(CHECK_REPLAY, Status.BLOCKED, "replay failed")
    if doc.get("result") != "pass":
        return _unknown(CHECK_REPLAY, "replay evidence has no result")
    at = _parse_time(doc.get("at"))
    if at is None or at.tzinfo is None:
        return _unknown(CHECK_REPLAY, "replay evidence has no timezone-aware 'at'")
    problem = _staleness(at, now, req.evidence_max_age_hours)
    if problem:
        return CheckResult(CHECK_REPLAY, Status.BLOCKED, f"replay evidence is {problem}")
    return CheckResult(CHECK_REPLAY, Status.PASS, "replay passed for the SHA")


def _report_problem(text: str, req: Request, expected_mode: str) -> CheckResult | None:
    """preflight 出力が「この deploy のもの」か。違えば CheckResult（名前は呼び出し側で付ける）。"""
    doc = _load_json(text)
    if doc is None:
        return CheckResult("", Status.UNKNOWN, "report is not JSON")
    if doc.get("target_sha") != req.target_sha:
        return CheckResult("", Status.BLOCKED, "report is for a different SHA")
    if doc.get("mode") != expected_mode:
        return CheckResult(
            "", Status.BLOCKED, f"report mode is {doc.get('mode')!r}, expected {expected_mode!r}"
        )
    if doc.get("overall") == Overall.BLOCKED.value:
        return CheckResult("", Status.BLOCKED, "report verdict is BLOCKED")
    return None


def _named(name: str, problem: CheckResult) -> CheckResult:
    return CheckResult(name, problem.status, problem.detail)


def _approved_report_problem(text: str, req: Request) -> CheckResult | None:
    """承認された出力は「承認以外が全て PASS」でなければならない（UNKNOWN を承認しない）。"""
    problem = _report_problem(text, req, report_mode(req))
    if problem is not None:
        return problem
    doc = _load_json(text) or {}
    results = doc.get("results")
    by_name = (
        {r.get("name"): r.get("status") for r in results if isinstance(r, dict)}
        if isinstance(results, list)
        else {}
    )
    not_pass = [
        n
        for n in PRE_DEPLOY_REQUIRED
        if n != CHECK_APPROVAL and by_name.get(n) != Status.PASS.value
    ]
    if not_pass:
        return CheckResult(
            "",
            Status.BLOCKED,
            f"approved report has checks that are not PASS: {', '.join(not_pass)}",
        )
    return None


_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _check_approval(req: Request, c: Collectors, now: datetime) -> CheckResult:
    """承認は SHA・日付・preflight 出力に束縛する。自己申告で、認証ではない（取り違えの防止）。"""
    path = req.approval_file or default_approval_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_APPROVAL, f"approval record absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(CHECK_APPROVAL, Status.BLOCKED, "approval is for a different SHA")
    at = _parse_time(doc.get("approved_at"))
    if not doc.get("approver") or at is None or at.tzinfo is None:
        return _unknown(
            CHECK_APPROVAL, "approval record lacks approver or a timezone-aware approved_at"
        )
    if not _bounded(req.evidence_max_age_hours, MAX_EVIDENCE_MAX_AGE_HOURS):
        return _unknown(
            CHECK_APPROVAL,
            f"--evidence-max-age-hours must be in (0, {MAX_EVIDENCE_MAX_AGE_HOURS:g}]",
        )
    stale = _staleness(at, now, req.evidence_max_age_hours)
    if stale:
        return CheckResult(CHECK_APPROVAL, Status.BLOCKED, f"approval is {stale}")
    bound = doc.get("report_sha256")
    if not isinstance(bound, str) or not _HEX64.match(bound):
        return _unknown(
            CHECK_APPROVAL, "approval lacks report_sha256 (bind it to the reviewed report)"
        )
    if req.approved_report is None:
        return _unknown(CHECK_APPROVAL, "no --approved-report given to verify report_sha256")
    text = c.read_text(req.approved_report)
    if text is None:
        return _unknown(CHECK_APPROVAL, f"approved report absent ({req.approved_report})")
    if _sha256(text) != bound:
        return CheckResult(
            CHECK_APPROVAL, Status.BLOCKED, "approved report differs from the one that was approved"
        )
    problem = _approved_report_problem(text, req)
    if problem is not None:
        return _named(CHECK_APPROVAL, problem)
    return CheckResult(CHECK_APPROVAL, Status.PASS, f"approved by {doc['approver']}")


# ---- deploy 後


def _check_at_target(sha: str, c: Collectors) -> CheckResult:
    revisions = c.container_revisions()
    if not revisions:
        return _unknown(CHECK_AT_TARGET, "no avp2 app container found")
    absent = _missing_services(CHECK_AT_TARGET, c, revisions)
    if absent is not None:
        return absent
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


def _pre_report_mode(req: Request) -> str:
    return "rollback-pre-deploy" if req.rollback else "pre-deploy"


def _read_pre_report(req: Request, c: Collectors) -> str | CheckResult:
    """--pre-report の本文。読めなければ UNKNOWN、この deploy のものでなければ BLOCKED/UNKNOWN。"""
    if not req.pre_report:
        return _unknown("", "no pre-deploy report (--pre-report)")
    text = c.read_text(req.pre_report)
    if text is None:
        return _unknown("", f"pre-deploy report absent ({req.pre_report})")
    problem = _report_problem(text, req, _pre_report_mode(req))
    return text if problem is None else problem


def _check_schedules_same(req: Request, c: Collectors) -> CheckResult:
    pre = _read_pre_report(req, c)
    if isinstance(pre, CheckResult):
        return _named(CHECK_SCHEDULES_SAME, pre)
    before = schedule_snapshot_from_report(pre)
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


def _check_data_from_report(text: str, name: str) -> dict[str, Any] | None:
    doc = _load_json(text) or {}
    for r in doc.get("results") or []:
        if isinstance(r, dict) and r.get("name") == name and r.get("status") == Status.PASS.value:
            data = r.get("data")
            return dict(data) if isinstance(data, dict) else None
    return None


def _check_alembic_head(req: Request, c: Collectors) -> CheckResult:
    prod = c.db_alembic_version()
    if req.rollback:
        return _check_alembic_unchanged(req, c, prod)
    head = alembic_head(c.migration_files(req.target_sha))
    if not prod or head is None:
        return _unknown(CHECK_ALEMBIC_HEAD, "alembic_version or target head is not determined")
    if prod != head:
        return CheckResult(CHECK_ALEMBIC_HEAD, Status.BLOCKED, f"production {prod} != head {head}")
    return CheckResult(CHECK_ALEMBIC_HEAD, Status.PASS, f"at head {head}")


def _check_alembic_unchanged(req: Request, c: Collectors, prod: str | None) -> CheckResult:
    """rollback の事後: migrate も downgrade も走っていない（DB は preflight 時点のまま）。"""
    pre = _read_pre_report(req, c)
    if isinstance(pre, CheckResult):
        return _named(CHECK_ALEMBIC_HEAD, pre)
    before = _check_data_from_report(pre, CHECK_ALEMBIC)
    if not prod or before is None or not before.get("production"):
        return _unknown(CHECK_ALEMBIC_HEAD, "alembic_version or its pre-deploy record is unknown")
    if prod != before["production"]:
        return CheckResult(
            CHECK_ALEMBIC_HEAD,
            Status.BLOCKED,
            f"production {prod} != {before['production']} recorded before the rollback "
            "(migrate/downgrade must not run in a rollback)",
        )
    return CheckResult(
        CHECK_ALEMBIC_HEAD,
        Status.PASS,
        f"unchanged at {prod} (ahead of the rolled-back code, accepted at preflight)",
    )


def _check_approval_binding(req: Request, c: Collectors) -> CheckResult:
    """deploy した preflight 出力が、承認されたものと同一（承認の ``report_sha256``）。"""
    path = req.approval_file or default_approval_path(req.target_sha)
    doc = _load_json(c.read_text(path))
    if doc is None:
        return _unknown(CHECK_APPROVAL_BINDING, f"approval record absent or not JSON ({path})")
    if doc.get("sha") != req.target_sha:
        return CheckResult(
            CHECK_APPROVAL_BINDING, Status.BLOCKED, "approval is for a different SHA"
        )
    pre = _read_pre_report(req, c)
    if isinstance(pre, CheckResult):
        return _named(CHECK_APPROVAL_BINDING, pre)
    if doc.get("report_sha256") != _sha256(pre):
        return CheckResult(
            CHECK_APPROVAL_BINDING,
            Status.BLOCKED,
            "the pre-deploy report is not the one the approval was bound to",
        )
    return CheckResult(
        CHECK_APPROVAL_BINDING, Status.PASS, "pre-deploy report matches the approval"
    )


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


_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _printable(value: str) -> str:
    """制御文字（ESC・改行・CR 等）を ``?`` にする。端末の操作と OVERALL 行の偽装を防ぐ。"""
    return _CONTROL.sub("?", value)


def render_text(report: Report) -> str:
    lines = [f"release preflight ({report.mode}) target={_printable(report.target_sha) or '-'}"]
    have = {r.name for r in report.results}
    for r in report.results:
        lines.append(f"[{r.status.value:^7}] {_printable(r.name)}: {_printable(r.detail)}")
    for name in report.required:
        if name not in have:
            lines.append(f"[UNKNOWN] {name}: not evaluated")
    lines.append("")
    lines.append(f"OVERALL: {report.overall.value}")
    return "\n".join(lines)
