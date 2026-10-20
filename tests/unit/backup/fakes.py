"""backup / restore の fake（本番にも docker にも触れない）。"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from contracts.backup import BACKUP_DATABASES
from infrastructure.backup.audit import AuditLog
from infrastructure.backup.objects import Downloaded, ObjectInfo
from infrastructure.backup.postgres import ROW_COUNT_SQL, PostgresAccess
from infrastructure.backup.runner import CmdResult, FileResult
from infrastructure.backup.service import BackupConfig, BackupDeps, BackupService

T0 = datetime(2026, 10, 21, 3, 0, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=1)
        return self.t


@dataclass
class FakeRunner:
    """docker exec <container> ... を解釈する。呼ばれた argv を全て記録する。"""

    tables: dict[str, dict[str, int]] = field(
        default_factory=lambda: {db: {"public.t": 3} for db in BACKUP_DATABASES}
    )
    alembic: list[str] = field(default_factory=lambda: ["0015"])
    fail_dump: set[str] = field(default_factory=set)
    empty_dump: set[str] = field(default_factory=set)
    toc_rc: int = 0
    offsite_rc: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def run(
        self, argv: Sequence[str], *, stdin_path: Path | None = None, timeout: float = 0
    ) -> CmdResult:
        self.calls.append(list(argv))
        a = list(argv)
        if a[0] == "offsite-cmd":
            return CmdResult(self.offsite_rc, "", "")
        if "psql" in a:
            db = a[a.index("-d") + 1]
            sql = a[-1]
            if "alembic_version" in sql:
                rev = self.alembic.pop(0) if len(self.alembic) > 1 else self.alembic[0]
                return CmdResult(0, rev + "\n", "")
            assert sql == ROW_COUNT_SQL
            return CmdResult(0, "".join(f"{t}|{n}\n" for t, n in self.tables[db].items()), "")
        if "pg_restore" in a and "--list" in a:
            return CmdResult(self.toc_rc, "", "bad toc" if self.toc_rc else "")
        if "--version" in a:
            return CmdResult(0, "pg_dump (PostgreSQL) 17.11\n", "")
        raise AssertionError(f"unexpected command {a}")

    def run_to_file(self, argv: Sequence[str], dest: Path, *, timeout: float = 0) -> FileResult:
        self.calls.append(list(argv))
        a = list(argv)
        if "pg_dumpall" in a:
            body = b"-- globals\n"
        else:
            db = a[a.index("-d") + 1]
            if db in self.fail_dump:
                return FileResult(1, "", 0, "pg_dump: error: boom")
            body = b"" if db in self.empty_dump else f"PGDMP {db}".encode()
        dest.write_bytes(body)
        return FileResult(0, hashlib.sha256(body).hexdigest(), len(body), "")


@dataclass
class FakeObjects:
    store: dict[str, dict[str, bytes]] = field(
        default_factory=lambda: {"artifacts": {"a/one.json": b'{"a":1}', "media/v.mp4": b"VIDEO"}}
    )
    size_lie: set[str] = field(default_factory=set)
    etag_lie: set[str] = field(default_factory=set)
    downloads: list[str] = field(default_factory=list)

    def bucket_exists(self, bucket: str) -> bool:
        return bucket in self.store

    def versioning(self, bucket: str) -> str:
        return "unversioned"

    def etag(self, body: bytes) -> str:
        return hashlib.md5(body, usedforsecurity=False).hexdigest()

    def list_objects(self, bucket: str) -> Iterator[ObjectInfo]:
        for key, body in sorted(self.store[bucket].items()):
            yield ObjectInfo(key, len(body), self.etag(body))

    def download(self, bucket: str, key: str, dest: Path) -> Downloaded:
        self.downloads.append(key)
        body = self.store[bucket][key]
        dest.write_bytes(body)
        size = len(body) + (1 if key in self.size_lie else 0)
        md5 = "0" * 32 if key in self.etag_lie else self.etag(body)
        return Downloaded(hashlib.sha256(body).hexdigest(), md5, size)


class Notes:
    def __init__(self, raises: bool = False) -> None:
        self.events: list[dict[str, Any]] = []
        self.raises = raises

    def notify(self, event: Any) -> None:
        self.events.append(dict(event))
        if self.raises:
            raise RuntimeError("webhook down")


@dataclass
class Harness:
    service: BackupService
    runner: FakeRunner
    objects: FakeObjects
    notes: Notes
    lines: list[str]
    db_root: Path
    object_root: Path
    clock: Clock


def make_backup(
    tmp_path: Path,
    *,
    runner: FakeRunner | None = None,
    objects: FakeObjects | None = None,
    notes: Notes | None = None,
    same_device: bool = False,
    allow_same_device: bool = False,
    offsite: list[str] | None = None,
    clock: Clock | None = None,
    single_root: bool = False,
) -> Harness:
    db_root = tmp_path / "db"
    object_root = db_root if single_root else tmp_path / "obj"
    db_root.mkdir(exist_ok=True)
    object_root.mkdir(exist_ok=True)
    runner = runner or FakeRunner()
    objects = objects or FakeObjects()
    notes = notes or Notes()
    clock = clock or Clock()
    lines: list[str] = []
    cfg = BackupConfig(
        db_root=db_root,
        object_root=object_root,
        buckets=("artifacts",),
        git_revision="1" * 40,
        git_dirty=False,
        allow_same_device=allow_same_device,
        offsite_command=offsite,
        tool_versions={"python": "3.13"},
    )
    deps = BackupDeps(
        runner=runner,
        postgres=PostgresAccess(runner, "avp2-postgres-1", "avp"),
        objects=objects,
        audit=AuditLog(None, write=lines.append, now=clock),
        notifier=notes,
        source_device=lambda role: 7 if same_device else {"db": 1, "objects": 2}[role],
        path_device=lambda p: 7 if same_device else 9,
        now=clock,
    )
    return Harness(
        BackupService(cfg, deps), runner, objects, notes, lines, db_root, object_root, clock
    )
