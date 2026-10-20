"""バックアップの実行（ADR-0051）。順序: PostgreSQL（3 DB）→ MinIO → manifest → 改名で確定。

一貫性の境界: PG を**先に**取り、MinIO を**後に**取る。Artifact は不変（INV-11）で、DB の行は
本体の put の**後**に commit される。よって dump に載る行の参照先は、後から取る MinIO の写しに
必ず存在する（逆順だと不可能）。MinIO にだけ在る本体は孤児で害がない。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.backup import (
    APP_DATABASE,
    BACKUP_DATABASES,
    BACKUP_ID_FORMAT,
)
from domain.backup.guards import destination_problems, safe_relative_key
from domain.backup.manifest import iso, new_manifest, validate_manifest
from infrastructure.backup import catalog
from infrastructure.backup.audit import AuditLog, Notifier
from infrastructure.backup.objects import ObjectSource, is_plain_md5_etag
from infrastructure.backup.postgres import CommandFailed, PostgresAccess
from infrastructure.backup.runner import CommandRunner


class BackupFailed(Exception):
    """バックアップを確定できなかった（partial は消して、manifest は作らない）。"""


@dataclass
class BackupConfig:
    db_root: Path
    object_root: Path
    buckets: tuple[str, ...]
    git_revision: str
    git_dirty: bool
    allow_same_device: bool = False
    quiesce: str = "none"
    offsite_command: list[str] | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)


@dataclass
class BackupDeps:
    runner: CommandRunner
    postgres: PostgresAccess
    objects: ObjectSource
    audit: AuditLog
    notifier: Notifier
    #: ("db" | "objects") -> 本番データのある device id / destination の device id
    source_device: Callable[[str], int]
    path_device: Callable[[Path], int]
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def _roles(cfg: BackupConfig) -> dict[str, Path]:
    return {"db": cfg.db_root, "objects": cfg.object_root}


def _unique_roots(cfg: BackupConfig) -> list[Path]:
    seen: list[Path] = []
    for p in (cfg.db_root, cfg.object_root):
        if p not in seen:
            seen.append(p)
    return seen


class BackupService:
    def __init__(self, cfg: BackupConfig, deps: BackupDeps) -> None:
        self._cfg = cfg
        self._d = deps

    # --- 公開 -------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """成功なら manifest を返す。失敗は ``BackupFailed``（audit・通知は済み）。"""
        d, cfg = self._d, self._cfg
        started = d.now()
        backup_id = started.strftime(BACKUP_ID_FORMAT)
        d.audit.emit("backup.started", backup_id=backup_id)
        partials: list[Path] = []
        try:
            same_device = self._check_destinations()
            partial = {role: root / f"{backup_id}.partial" for role, root in _roles(cfg).items()}
            for p in dict.fromkeys(partial.values()):
                p.mkdir(mode=0o700)
                partials.append(p)
            files: dict[str, dict[str, Any]] = {}
            databases, pg_times, alembic = self._backup_postgres(partial["db"], files)
            objects = self._backup_objects(partial["objects"], backup_id, files)
            finished = d.now()
            manifest = new_manifest(
                backup_id=backup_id,
                started_at=started,
                finished_at=finished,
                git_revision=cfg.git_revision,
                git_dirty=cfg.git_dirty,
                alembic_revision=alembic,
                tool_versions=self._tool_versions(),
                databases=databases,
                objects=objects,
                files=files,
                consistency={
                    "order": "postgres_then_minio",
                    "postgres_started_at": pg_times[0],
                    "postgres_finished_at": pg_times[1],
                    "minio_started_at": objects["started_at"],
                    "minio_finished_at": objects["finished_at"],
                    "quiesce": cfg.quiesce,
                },
                destination={
                    "db_root": str(cfg.db_root),
                    "object_root": str(cfg.object_root),
                    "same_device_as_source": same_device,
                    "single_root": cfg.db_root == cfg.object_root,
                },
                offsite={"configured": cfg.offsite_command is not None, "ok": None},
            )
            problems = validate_manifest(manifest)
            if problems:
                raise BackupFailed(f"manifest invalid: {problems}")
            for p in dict.fromkeys(partial.values()):
                catalog.write_manifest(p, manifest)
            for root in _unique_roots(cfg):
                if (root / backup_id).exists():
                    raise BackupFailed(f"{root / backup_id} already exists")
            for p in dict.fromkeys(partial.values()):
                os.replace(p, p.with_name(backup_id))
            partials.clear()
        except BackupFailed as exc:
            self._fail(backup_id, partials, str(exc))
            raise
        except Exception as exc:  # 想定外も握りつぶさず、同じ失敗経路に載せる
            self._fail(backup_id, partials, f"{type(exc).__name__}: {exc}")
            raise BackupFailed(f"{type(exc).__name__}: {exc}") from exc
        d.audit.emit(
            "backup.completed",
            backup_id=backup_id,
            objects=manifest["objects"]["count"],
            bytes=manifest["objects"]["bytes"],
            restore_verified=False,
        )
        self._offsite(backup_id)
        return manifest

    # --- 内部 -------------------------------------------------------------------
    def _fail(self, backup_id: str, partials: list[Path], error: str) -> None:
        for p in partials:
            if p.name == f"{backup_id}.partial" and p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
        self._d.audit.emit("backup.failed", backup_id=backup_id, error=error)
        self._notify({"event": "backup.failed", "backup_id": backup_id, "error": error})

    def _notify(self, event: Mapping[str, Any]) -> None:
        try:
            self._d.notifier.notify(event)
        except Exception as exc:  # noqa: BLE001 - 通知の失敗でバックアップの結果を変えない
            self._d.audit.emit("backup.notify_failed", error=type(exc).__name__)

    def _check_destinations(self) -> bool:
        cfg, d = self._cfg, self._d
        problems: list[str] = []
        for role, root in _roles(cfg).items():
            if not root.is_dir():
                raise BackupFailed(f"{role} root does not exist: {root}")
            problems += destination_problems(
                role,
                d.path_device(root),
                d.source_device(role),
                allow_same_device=cfg.allow_same_device,
            )
        if problems:
            raise BackupFailed("; ".join(problems))
        return any(d.path_device(r) == d.source_device(k) for k, r in _roles(cfg).items())

    def _tool_versions(self) -> dict[str, str]:
        versions = dict(self._cfg.tool_versions)
        try:
            versions["pg_dump"] = self._d.postgres.version()
        except CommandFailed as exc:
            raise BackupFailed(str(exc)) from exc
        return versions

    def _backup_postgres(
        self, out: Path, files: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], tuple[str, str], str]:
        pg, d = self._d.postgres, self._d
        (out / "pg").mkdir(mode=0o700)
        pg_started = iso(d.now())
        databases: dict[str, dict[str, Any]] = {}
        try:
            alembic_before = pg.alembic_revision(APP_DATABASE)
            for name in BACKUP_DATABASES:
                before = pg.row_counts(name)
                dump = out / "pg" / f"{name}.dump"
                res = pg.dump(name, dump)
                if res.returncode != 0 or res.size == 0:
                    raise BackupFailed(
                        f"pg_dump {name} failed: rc={res.returncode} {res.stderr.strip()[:200]}"
                    )
                toc = pg.list_toc(dump)
                if toc.returncode != 0:
                    raise BackupFailed(f"pg_dump {name}: dump is unreadable: {toc.stderr[:200]}")
                after = pg.row_counts(name)
                rel = f"pg/{name}.dump"
                dump.chmod(0o600)
                files[rel] = {"sha256": res.sha256, "bytes": res.size, "root": "db"}
                databases[name] = {
                    "file": rel,
                    "sha256": res.sha256,
                    "bytes": res.size,
                    "finished_at": iso(d.now()),
                    "rows": {
                        t: {"before": before.get(t, 0), "after": after.get(t, 0)}
                        for t in sorted(set(before) | set(after))
                    },
                }
                d.audit.emit("backup.database_dumped", database=name, bytes=res.size)
            gl = out / "pg" / "globals.sql"
            res = pg.dump_globals(gl)
            if res.returncode != 0 or res.size == 0:
                raise BackupFailed(f"pg_dumpall --globals-only failed: rc={res.returncode}")
            gl.chmod(0o600)
            files["pg/globals.sql"] = {"sha256": res.sha256, "bytes": res.size, "root": "db"}
            alembic_after = pg.alembic_revision(APP_DATABASE)
        except CommandFailed as exc:
            raise BackupFailed(str(exc)) from exc
        if alembic_before != alembic_after:
            raise BackupFailed(
                f"alembic revision changed during backup: {alembic_before}->{alembic_after}"
            )
        return databases, (pg_started, iso(d.now())), alembic_after

    def _previous(self) -> tuple[Path, dict[str, Any]] | None:
        """直前の完了した backup（hardlink で再利用する元）。"""
        root = self._cfg.object_root
        for backup_id in reversed(catalog.list_backup_ids(root)):
            m = catalog.read_manifest(root / backup_id)
            if m is not None and validate_manifest(m) == []:
                return root / backup_id, m
        return None

    def _backup_objects(
        self, out: Path, backup_id: str, files: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        src, d = self._d.objects, self._d
        started = iso(d.now())
        prev = self._previous()
        buckets: dict[str, dict[str, Any]] = {}
        total_count = total_bytes = reused = 0
        for bucket in self._cfg.buckets:
            if not src.bucket_exists(bucket):
                raise BackupFailed(f"bucket does not exist: {bucket}")
            count = size_sum = 0
            for obj in src.list_objects(bucket):
                rel = safe_relative_key(f"minio/{bucket}", obj.key)
                if rel is None:
                    raise BackupFailed(f"unsafe object key in {bucket}: {obj.key!r}")
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                entry = self._reuse(prev, rel, obj.size, obj.etag, dest)
                if entry is None:
                    got = src.download(bucket, obj.key, dest)
                    if got.size != obj.size:
                        raise BackupFailed(
                            f"{bucket}/{obj.key}: size {got.size} != listed {obj.size}"
                        )
                    if is_plain_md5_etag(obj.etag) and got.md5 != obj.etag.strip('"'):
                        raise BackupFailed(f"{bucket}/{obj.key}: transfer checksum (etag) mismatch")
                    entry = {"sha256": got.sha256, "bytes": got.size}
                else:
                    reused += 1
                dest.chmod(0o600)
                files[rel] = {**entry, "etag": obj.etag, "root": "objects"}
                count += 1
                size_sum += obj.size
            buckets[bucket] = {
                "count": count,
                "bytes": size_sum,
                "versioning": src.versioning(bucket),
            }
            total_count += count
            total_bytes += size_sum
            d.audit.emit("backup.bucket_copied", bucket=bucket, objects=count, bytes=size_sum)
        return {
            "started_at": started,
            "finished_at": iso(d.now()),
            "count": total_count,
            "bytes": total_bytes,
            "reused_from_previous": reused,
            "buckets": buckets,
        }

    def _reuse(
        self, prev: tuple[Path, dict[str, Any]] | None, rel: str, size: int, etag: str, dest: Path
    ) -> dict[str, Any] | None:
        """key・size・etag が前回と同じ**不変**の本体は hardlink で共有する（容量を増やさない）。"""
        if prev is None:
            return None
        prev_dir, m = prev
        old = m.get("files", {}).get(rel)
        if not old or old.get("bytes") != size or not etag or old.get("etag") != etag:
            return None
        src = prev_dir / rel
        try:
            if src.stat().st_size != size:
                return None
            dest.unlink(missing_ok=True)
            os.link(src, dest)
        except OSError:
            return None
        return {"sha256": old["sha256"], "bytes": size}

    def _offsite(self, backup_id: str) -> None:
        argv = self._cfg.offsite_command
        if not argv:
            return
        cmd = [
            a.replace("{db_set}", str(self._cfg.db_root / backup_id)).replace(
                "{object_set}", str(self._cfg.object_root / backup_id)
            )
            for a in argv
        ]
        res = self._d.runner.run(cmd)
        ok = res.returncode == 0
        for root in _unique_roots(self._cfg):
            m = catalog.read_manifest(root / backup_id)
            if m is not None:
                m["offsite"] = {"configured": True, "ok": ok, "at": iso(self._d.now())}
                catalog.write_manifest(root / backup_id, m)
        if not ok:
            self._d.audit.emit("backup.offsite_failed", backup_id=backup_id, rc=res.returncode)
            self._notify({"event": "backup.offsite_failed", "backup_id": backup_id})
            raise BackupFailed(
                f"offsite command failed rc={res.returncode} (local backup is intact)"
            )
        self._d.audit.emit("backup.offsite_done", backup_id=backup_id)
