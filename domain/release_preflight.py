"""release preflight の純粋な判定（ADR-0050 / INV-65）。

I/O は一切しない。外界は ``Collectors`` Protocol として注入され、collector が投げた例外・不在は
**すべて UNKNOWN** に変換する。UNKNOWN も欠けた必須チェックも READY にはならない。
実際の collector は ``infrastructure/release/collectors.py``（読み取りだけ）、CLI は
``scripts/release-preflight.py``。
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

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
    CHECK_AT_TARGET,
    CHECK_POLLERS,
    CHECK_SCHEDULES_SAME,
    CHECK_ALEMBIC_HEAD,
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
    def adr_files(self, rev: str) -> Sequence[str]: ...
    def running_workflow_count(self) -> int: ...
    def pending_reservation_count(self) -> int: ...
    def schedule_states(self) -> Mapping[str, Mapping[str, object]]: ...
    def required_queues(self) -> Sequence[str]: ...
    def queues_without_poller(self, queues: Sequence[str]) -> Sequence[str]: ...
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
    results: list[CheckResult] = [sha_result]
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
_CI_GOOD = {"success", "skipped", "neutral"}


def _check_ci(sha: str, c: Collectors) -> CheckResult:
    runs = list(c.ci_check_runs(sha))
    if not runs:
        return _unknown(CHECK_CI, "no check runs reported for the SHA")
    failed = [r.name for r in runs if r.status == "completed" and r.conclusion in _CI_BAD]
    if failed:
        return CheckResult(CHECK_CI, Status.BLOCKED, f"CI not green: {', '.join(sorted(failed))}")
    pending = [r.name for r in runs if r.status != "completed" or r.conclusion not in _CI_GOOD]
    if pending:
        return _unknown(CHECK_CI, f"not finished or unrecognized: {', '.join(sorted(pending))}")
    return CheckResult(CHECK_CI, Status.PASS, f"{len(runs)} check runs succeeded")


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


_CONTRACT_MARKER = re.compile(
    r"^\s*#\s*contract-migration:\s*approved\s+ADR-(\d{4})\b", re.MULTILINE
)
_DESTRUCTIVE_CALLS = {"drop_column", "drop_table", "rename_table"}
_DESTRUCTIVE_SQL = re.compile(
    r"\b(DROP\s+(TABLE|COLUMN)|RENAME|TRUNCATE)\b|ALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL",
    re.IGNORECASE,
)


def _kwargs_has(node: ast.Call, key: str) -> bool:
    return any(
        kw.arg == key for sub in ast.walk(node) if isinstance(sub, ast.Call) for kw in sub.keywords
    )


def _not_null_without_default(node: ast.Call) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            for kw in sub.keywords:
                if (
                    kw.arg == "nullable"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is False
                ):
                    return not _kwargs_has(node, "server_default")
    return False


def destructive_findings(source: str) -> list[str]:
    """``upgrade()`` の中だけを見る（downgrade の drop は expand migration でも普通にある）。"""
    tree = ast.parse(source)
    findings: list[str] = []
    for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "upgrade"):
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = (
                    node.func.attr
                    if isinstance(node.func, ast.Attribute)
                    else getattr(node.func, "id", "")
                )
                if name in _DESTRUCTIVE_CALLS:
                    findings.append(f"{name} (line {node.lineno})")
                elif name == "alter_column":
                    if _kwargs_has(node, "new_column_name"):
                        findings.append(f"rename column (line {node.lineno})")
                    elif _not_null_without_default(node):
                        findings.append(f"SET NOT NULL without server_default (line {node.lineno})")
                elif name == "add_column" and _not_null_without_default(node):
                    findings.append(f"NOT NULL column without server_default (line {node.lineno})")
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and _DESTRUCTIVE_SQL.search(node.value)
            ):
                findings.append(f"destructive SQL (line {node.lineno})")
    return findings


def _check_migration(sha: str, c: Collectors, deployed: str | None) -> CheckResult:
    if deployed is None:
        return _unknown(CHECK_MIGRATION, "deployed revision is not determined")
    base = c.migration_files(deployed)
    target = c.migration_files(sha)
    new = {name: text for name, text in target.items() if name not in base}
    if not new:
        return CheckResult(CHECK_MIGRATION, Status.PASS, "no new migration")
    blocked: list[str] = []
    for name, text in sorted(new.items()):
        try:
            found = destructive_findings(text)
        except SyntaxError:
            return _unknown(CHECK_MIGRATION, f"{name} cannot be parsed")
        if not found:
            continue
        marker = _CONTRACT_MARKER.search(text)
        if marker is not None and any(
            a.startswith(marker.group(1) + "-") for a in c.adr_files(sha)
        ):
            continue
        blocked.append(f"{name}: {'; '.join(found)}")
    if blocked:
        return CheckResult(
            CHECK_MIGRATION,
            Status.BLOCKED,
            "contract migration without an approved ADR marker: " + " | ".join(blocked),
        )
    return CheckResult(CHECK_MIGRATION, Status.PASS, f"{len(new)} new migration(s), expand-only")


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
