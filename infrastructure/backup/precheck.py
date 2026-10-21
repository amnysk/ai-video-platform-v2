"""本番 backup を承認する前の read-only 点検（ADR-0051）。

本番へ送るのは ``docker inspect`` / SQL（SELECT のみ。PostgresAccess 経由）/ pg_dump のバージョン確認 / S3 の
list・stat だけ。何も作らず、書かず、audit も残さない（root が無くても作らない）。
secret は**名前だけ**を扱い、値は出力・例外・ログに入れない。
判定は ``domain/backup/precheck.py``。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from contracts.backup import (
    APP_DATABASE,
    BACKUP_DATABASES,
    ENV_DB_ROOT,
    ENV_OBJECT_ROOT,
)
from domain.backup.guards import destination_problems, root_overlap_problems
from domain.backup.precheck import (
    Check,
    Status,
    ddl_like_sessions,
    estimate,
    next_safe_start,
    overall,
    pg_major_from_dump_version,
    pg_major_from_num,
    required_bytes,
    schedule_conflicts,
)
from infrastructure.backup.mounts import env_names, mount_source
from infrastructure.backup.objects import ObjectSource
from infrastructure.backup.postgres import CommandFailed, PostgresAccess
from infrastructure.backup.runner import CommandRunner

PG_DATA_DEST = "/var/lib/postgresql/data"
MINIO_DATA_DEST = "/data"
#: backup が使う credential の**名前**（値は扱わない）。compose の既定値がある名前は既定でも動く
REQUIRED_CREDENTIALS = ("MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "POSTGRES_USER")
KMS_PREFIX = "MINIO_KMS_"
LONG_RUNNING_SECONDS = 60.0
MB = 1024 * 1024


class Fs(Protocol):
    """read-only のファイルシステム観測。nearest existing ancestor で答える。"""

    def exists(self, path: str) -> bool: ...
    def device(self, path: str) -> int: ...
    def free_bytes(self, path: str) -> int: ...
    def writable(self, path: str) -> bool: ...


class OsFs:
    @staticmethod
    def _anchor(path: str) -> str:
        p = Path(os.path.realpath(path))
        while not p.exists() and p != p.parent:
            p = p.parent
        return str(p)

    def exists(self, path: str) -> bool:
        return Path(path).exists()

    def device(self, path: str) -> int:
        return os.stat(self._anchor(path)).st_dev

    def free_bytes(self, path: str) -> int:
        st = os.statvfs(self._anchor(path))
        return st.f_bavail * st.f_frsize

    def writable(self, path: str) -> bool:
        return os.access(self._anchor(path), os.W_OK | os.X_OK)


@dataclass(frozen=True)
class PrecheckConfig:
    db_root: str | None
    object_root: str | None
    buckets: tuple[str, ...]
    postgres_container: str
    minio_container: str
    protected_paths: tuple[str, ...]
    daily_cron: str
    schedule_timezone: str
    watchdog_cron: str
    #: credential 名 -> "environment" | "env-file" | "built-in default"（値は持たない）
    credential_sources: Mapping[str, str]


@dataclass
class PrecheckDeps:
    runner: CommandRunner
    postgres: PostgresAccess
    objects: ObjectSource | None
    fs: Fs
    now: Callable[[], datetime]
    repo_alembic_head: Callable[[], str]


def _safe(exc: BaseException) -> str:
    """例外は型名と短い文だけ（secret を含み得る接続文字列を出さない）。"""
    return type(exc).__name__


class Prechecker:
    def __init__(self, cfg: PrecheckConfig, deps: PrecheckDeps) -> None:
        self._c, self._d = cfg, deps
        self._checks: list[Check] = []
        self._sizes: dict[str, int] | None = None
        self._objects: dict[str, int] | None = None  # {"count", "bytes"}
        self._devices: dict[str, int | None] = {}
        self._sources: dict[str, str] = {}

    def run(self) -> dict[str, Any]:
        self._roots()
        self._postgres()
        self._minio()
        self._credentials()
        self._free_space()
        self._migration()
        est = self._estimate()
        self._schedule(est)
        return {
            "schema": 1,
            "generated_at": self._d.now().isoformat(),
            "read_only": True,
            "verdict": overall(self._checks).value,
            "checks": [
                {"id": c.id, "status": c.status.value, "detail": c.detail, "data": c.data}
                for c in self._checks
            ],
            "estimate": est,
        }

    def _add(self, id_: str, status: Status, detail: str, **data: Any) -> None:
        self._checks.append(Check(id_, status, detail, data))

    # (a) roots ------------------------------------------------------------------------
    def _source_device(self, role: str) -> int | None:
        container, dest = {
            "db": (self._c.postgres_container, PG_DATA_DEST),
            "objects": (self._c.minio_container, MINIO_DATA_DEST),
        }[role]
        src = mount_source(self._d.runner, container, dest)
        if src is None:
            return None
        try:
            self._sources[role] = src
            return self._d.fs.device(src)
        except OSError:
            return None

    def _roots(self) -> None:
        roles = {
            "db": (self._c.db_root, ENV_DB_ROOT),
            "objects": (self._c.object_root, ENV_OBJECT_ROOT),
        }
        for role, (root, env_name) in roles.items():
            if not root:
                self._add(f"root_{role}", Status.FAIL, f"{env_name} is not set (no default)")
                self._devices[role] = None
                continue
            problems: list[str] = []
            unknown: list[str] = []
            exists = self._d.fs.exists(root)
            writable = self._d.fs.writable(root)
            if not writable:
                problems.append("not writable by the current user (nearest existing parent)")
            problems += root_overlap_problems(role, root, self._c.protected_paths)
            src_dev = self._source_device(role)
            dst_dev: int | None
            try:
                dst_dev = self._d.fs.device(root)
            except OSError:
                dst_dev = None
            self._devices[role] = dst_dev
            if src_dev is None or dst_dev is None:
                unknown.append("cannot prove the destination device differs from the source")
            else:
                problems += destination_problems(role, dst_dev, src_dev, allow_same_device=False)
            status = Status.FAIL if problems else Status.UNKNOWN if unknown else Status.PASS
            self._add(
                f"root_{role}",
                status,
                "; ".join(problems + unknown) or "ok",
                path=root,
                exists=exists,
                would_be_created=not exists,
                writable=writable,
                protects_mount_source=self._sources.get(role),
                destination_device=dst_dev,
                source_device=src_dev,
            )

    # (c) postgres ---------------------------------------------------------------------
    def _postgres(self) -> None:
        pg = self._d.postgres
        try:
            dump_version = pg.version()
            rows = pg.query_rows(
                APP_DATABASE,
                "SELECT datname, pg_database_size(datname) FROM pg_database "
                "WHERE NOT datistemplate ORDER BY 1",
            )
            num = pg.query_rows(APP_DATABASE, "SELECT current_setting('server_version_num')")
        except CommandFailed as exc:
            self._add("postgres", Status.FAIL, f"postgres container not reachable: {_safe(exc)}")
            return
        sizes = {r[0]: int(r[1]) for r in rows}
        missing = [d for d in BACKUP_DATABASES if d not in sizes]
        self._sizes = None if missing else {d: sizes[d] for d in BACKUP_DATABASES}
        server = pg_major_from_num(num[0][0]) if num and num[0] else None
        client = pg_major_from_dump_version(dump_version)
        if server is None or client is None:
            self._add(
                "postgres_version", Status.UNKNOWN, "cannot parse versions", client=dump_version
            )
        elif client < server:
            self._add(
                "postgres_version",
                Status.FAIL,
                f"pg_dump {client} < server {server}",
                client=client,
                server=server,
            )
        else:
            self._add(
                "postgres_version",
                Status.PASS,
                f"pg_dump {client} >= server {server}",
                client=client,
                server=server,
            )
        self._add(
            "postgres_databases",
            Status.FAIL if missing else Status.PASS,
            f"missing: {missing}" if missing else "all 3 target databases exist",
            sizes_bytes={d: sizes[d] for d in BACKUP_DATABASES if d in sizes},
            other_databases=sorted(set(sizes) - set(BACKUP_DATABASES)),
        )
        self._activity()

    def _activity(self) -> None:
        pg = self._d.postgres
        try:
            rows = pg.query_rows(
                APP_DATABASE,
                "SELECT pid, state, coalesce(extract(epoch FROM now() - xact_start)::bigint, 0), "
                "replace(left(query, 40), E'\\n', ' ') FROM pg_stat_activity "
                "WHERE pid <> pg_backend_pid() AND state <> 'idle' "
                "AND backend_type = 'client backend'",
            )
            locks = pg.query_rows(
                APP_DATABASE,
                "SELECT count(*) FROM pg_locks WHERE granted AND locktype = 'relation' "
                "AND mode = 'AccessExclusiveLock' AND pid <> pg_backend_pid()",
            )
        except CommandFailed as exc:
            self._add(
                "postgres_activity", Status.UNKNOWN, f"cannot read pg_stat_activity: {_safe(exc)}"
            )
            return
        ddl = ddl_like_sessions(rows, min_seconds=LONG_RUNNING_SECONDS)
        held = int(locks[0][0]) if locks and locks[0] else 0
        problems = []
        if ddl:
            problems.append(f"{len(ddl)} long-running DDL-like session(s)")
        if held:
            problems.append(f"{held} AccessExclusiveLock(s) held")
        self._add(
            "postgres_activity",
            Status.FAIL if problems else Status.PASS,
            "; ".join(problems) or f"no long DDL/exclusive locks ({len(rows)} active session(s))",
            active_sessions=len(rows),
            ddl_sessions=ddl,
            access_exclusive_locks=held,
        )

    # (d) minio ------------------------------------------------------------------------
    def _minio(self) -> None:
        objs = self._d.objects
        if objs is None:
            self._add("minio", Status.UNKNOWN, "no S3 client (credentials/endpoint not usable)")
            return
        total = count = 0
        versions: dict[str, str] = {}
        try:
            for bucket in self._c.buckets:
                if not objs.bucket_exists(bucket):
                    self._add("minio_bucket", Status.FAIL, f"bucket does not exist: {bucket}")
                    return
                versions[bucket] = objs.versioning(bucket)
                for o in objs.list_objects(bucket):
                    count += 1
                    total += o.size
        except Exception as exc:  # noqa: BLE001 - 接続断・認証失敗は型名だけ報告する
            self._add("minio", Status.FAIL, f"S3 API not usable: {_safe(exc)}")
            return
        self._objects = {"count": count, "bytes": total}
        self._add(
            "minio_bucket",
            Status.PASS,
            f"{count} objects, {total} bytes",
            buckets=list(self._c.buckets),
            count=count,
            bytes=total,
            versioning=versions,
        )
        versioning = ",".join(f"{b}={v}" for b, v in versions.items())
        if any(v.startswith("unknown") for v in versions.values()):
            self._add("minio_versioning", Status.UNKNOWN, f"versioning status: {versioning}")
        else:
            self._add(
                "minio_versioning", Status.PASS, f"versioning: {versioning}", versioning=versions
            )
        names = env_names(self._d.runner, self._c.minio_container)
        if names is None:
            self._add("minio_kms", Status.UNKNOWN, "cannot inspect MinIO container env")
            return
        kms = [n for n in names if n.startswith(KMS_PREFIX)]
        if kms:
            self._add(
                "minio_kms",
                Status.UNKNOWN,
                "server-side encryption env present: copies on the backup disk are plaintext; "
                "owner decides if acceptable",
                env_names=kms,
            )
        else:
            self._add("minio_kms", Status.PASS, "no MINIO_KMS_* env on the container", env_names=[])

    # (e) credentials ------------------------------------------------------------------
    def _credentials(self) -> None:
        sources = {n: self._c.credential_sources.get(n) for n in REQUIRED_CREDENTIALS}
        missing = [n for n, s in sources.items() if not s]
        defaults = [n for n, s in sources.items() if s == "built-in default"]
        self._add(
            "credentials",
            Status.FAIL if missing else Status.PASS,
            f"missing: {missing}"
            if missing
            else ("present (names only); built-in default in use for: " + ", ".join(defaults))
            if defaults
            else "present (names only)",
            sources={n: s or "missing" for n, s in sources.items()},
        )

    # (b) free space -------------------------------------------------------------------
    def _free_space(self) -> None:
        if self._sizes is None or self._objects is None:
            self._add("free_space", Status.UNKNOWN, "source sizes unknown (see postgres/minio)")
            return
        need = {
            "db": required_bytes(sum(self._sizes.values())),
            "objects": required_bytes(self._objects["bytes"]),
        }
        by_device: dict[int, dict[str, Any]] = {}
        for role, root in (("db", self._c.db_root), ("objects", self._c.object_root)):
            dev = self._devices.get(role)
            if not root or dev is None:
                self._add("free_space", Status.UNKNOWN, f"{role} root device unknown")
                return
            slot = by_device.setdefault(dev, {"root": root, "need": 0, "roles": []})
            slot["need"] += need[role]
            slot["roles"].append(role)
        problems = []
        report = []
        for slot in by_device.values():
            free = self._d.fs.free_bytes(slot["root"])
            report.append(
                {
                    "roles": slot["roles"],
                    "root": slot["root"],
                    "free_bytes": free,
                    "required_bytes": slot["need"],
                }
            )
            if free < slot["need"]:
                problems.append(f"{'+'.join(slot['roles'])}: free {free} < required {slot['need']}")
        self._add(
            "free_space",
            Status.FAIL if problems else Status.PASS,
            "; ".join(problems) or "enough free space (required = sources x 2)",
            devices=report,
        )

    # (f) migration / schedule -----------------------------------------------------------
    def _migration(self) -> None:
        try:
            db_rev = self._d.postgres.alembic_revision(APP_DATABASE)
        except CommandFailed as exc:
            self._add("alembic", Status.FAIL, f"alembic_version not readable: {_safe(exc)}")
            return
        head = self._d.repo_alembic_head()
        if db_rev == head:
            self._add("alembic", Status.PASS, f"DB at repo head {head}", db=db_rev, repo_head=head)
        else:
            self._add(
                "alembic",
                Status.UNKNOWN,
                f"DB {db_rev} != repo head {head}: migration/deploy may be pending or running",
                db=db_rev,
                repo_head=head,
            )

    def _estimate(self) -> dict[str, Any]:
        if self._sizes is None or self._objects is None:
            return {"seconds": None}
        est = estimate(
            db_bytes=sum(self._sizes.values()),
            object_bytes=self._objects["bytes"],
            object_count=self._objects["count"],
        )
        est["load"] = {
            "postgres": "pg_dump x3 databases (+ globals): one REPEATABLE READ snapshot per dump, "
            "ACCESS SHARE locks on all tables (blocks DDL/migrations, not DML), "
            "sequential reads on the pgdata disk",
            "minio": "sequential GET of every object (read IO on the MinIO data disk; "
            "no writes to MinIO)",
            "destination_writes_bytes_upper_bound": sum(self._sizes.values())
            + self._objects["bytes"],
        }
        return est

    def _schedule(self, est: dict[str, Any]) -> None:
        now = self._d.now()
        secs = est.get("seconds")
        if secs is None:
            self._add("schedule_window", Status.UNKNOWN, "duration unknown")
            return
        c = self._c
        windows = (c.daily_cron, c.schedule_timezone, c.watchdog_cron)
        conflicts = schedule_conflicts(now, secs, *windows)
        safe = next_safe_start(now, secs, *windows)
        self._add(
            "schedule_window",
            Status.FAIL if conflicts else Status.PASS,
            "; ".join(conflicts) or "starting now does not overlap the daily/watchdog windows",
            daily=f"{c.daily_cron} {c.schedule_timezone}",
            watchdog=c.watchdog_cron,
            next_safe_start=safe.isoformat() if safe else None,
        )


def render_human(report: Mapping[str, Any]) -> str:
    lines = [f"backup precheck (read-only) verdict: {report['verdict']}"]
    for c in report["checks"]:
        lines.append(f"  [{c['status']:7}] {c['id']}: {c['detail']}")
    est = report.get("estimate", {})
    if est.get("seconds") is not None:
        lines.append(f"  estimated duration: {est['seconds']} s (assumptions in JSON)")
    return "\n".join(lines)
