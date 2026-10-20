"""restore drill（ADR-0051）。**隔離した復旧先にだけ**書く。本番には何も実行しない。

流れ: 復旧先の拒否判定 → manifest 読み込み → 全ファイルを作業領域へ複写して sha256 照合 →
使い捨て PG コンテナ（``--network none``）へ pg_restore → 行数・alembic・オブジェクト・参照整合 →
証跡を manifest へ（``apply_drill``）→ コンテナと作業ディレクトリを片付ける。
docker へ渡す名前は全て ``target.container_name``（拒否判定を通ったもの）だけ。
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from contracts.backup import (
    APP_DATABASE,
    ARTIFACT_REFERENCE_TABLES,
    RESTORE_DB_PREFIX,
    RESTORE_POSTGRES_IMAGE,
)
from domain.backup.guards import RestoreTarget, assert_isolated
from domain.backup.manifest import (
    DrillEvidence,
    apply_drill,
    reference_problems,
    row_count_problems,
    validate_manifest,
)
from infrastructure.backup import catalog
from infrastructure.backup.audit import AuditLog, Notifier
from infrastructure.backup.postgres import PostgresAccess
from infrastructure.backup.runner import CommandRunner
from infrastructure.workdir import MINIO_DATA_DIR

RESTORE_PG_USER = "restore"
READY_ATTEMPTS = 60
MAX_LISTED_PROBLEMS = 20
CHUNK = 1024 * 1024


class DrillAborted(Exception):
    """drill を始められなかった（manifest が読めない等）。証跡は書かない。"""


@dataclass
class DrillDeps:
    runner: CommandRunner
    audit: AuditLog
    notifier: Notifier
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    image: str = RESTORE_POSTGRES_IMAGE
    #: 復旧先として使えない本番データの置き場（backup root は run() が足す）
    protected_paths: tuple[str, ...] = (str(MINIO_DATA_DIR),)


def restore_db_name(db: str) -> str:
    return RESTORE_DB_PREFIX + db


def _copy_and_hash(src: Path, dest: Path) -> str:
    digest = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, dest.open("xb") as fout:
        for chunk in iter(lambda: fin.read(CHUNK), b""):
            digest.update(chunk)
            fout.write(chunk)
    return digest.hexdigest()


def _check(passed: bool, **detail: Any) -> dict[str, Any]:
    return {"passed": passed, "detail": detail}


class RestoreDrill:
    def __init__(
        self,
        db_root: Path,
        object_root: Path,
        target: RestoreTarget,
        deps: DrillDeps,
    ) -> None:
        self._db_root, self._object_root = db_root, object_root
        self._target, self._d = target, deps
        self._versions: dict[str, str] = {}

    def _roots(self) -> list[Path]:
        return (
            [self._db_root]
            if self._db_root == self._object_root
            else [self._db_root, self._object_root]
        )

    # --- 公開 -------------------------------------------------------------------
    def run(self, backup_id: str) -> dict[str, Any]:
        """演習の結果を manifest に書いて返す。``restore_verified`` は pass のときだけ True。

        ``ProductionTargetRefused``（復旧先が本番）は何も実行する前に上がる。
        """
        t, d = self._target, self._d
        assert_isolated(
            t,
            protected_paths=[str(self._db_root), str(self._object_root), *d.protected_paths],
        )
        manifest = self._load(backup_id)
        d.audit.emit("restore.started", backup_id=backup_id, container=t.container_name)
        work = Path(t.work_dir)
        work.mkdir(mode=0o700, parents=True, exist_ok=False)
        container_started = False
        checks: dict[str, dict[str, Any]] = {}
        cleanup_ok = True
        try:
            checks["files_at_rest_checksums"], copied = self._copy_files(manifest, work)
            if checks["files_at_rest_checksums"]["passed"]:
                container_started = True
                checks.update(self._restore_and_verify(manifest, work, copied))
        except Exception as exc:  # noqa: BLE001 - 想定外も「失敗の証跡」にする
            checks["drill_error"] = _check(False, error=f"{type(exc).__name__}: {exc}")
        finally:
            cleanup_ok = self._cleanup(work, container_started)
        evidence = DrillEvidence(
            backup_id=backup_id,
            at=d.now(),
            target={
                "container": t.container_name,
                "image": d.image,
                "network": "none",
                "databases": list(t.databases),
                "work_dir": t.work_dir,
                "cleanup_ok": cleanup_ok,
            },
            checks=checks,
            tool_versions=self._versions,
        )
        updated = apply_drill(manifest, evidence)
        for root in self._roots():
            catalog.write_manifest(root / backup_id, updated)
        ok = updated["restore_verified"] is True
        d.audit.emit(
            "restore.completed" if ok else "restore.failed",
            backup_id=backup_id,
            restore_verified=ok,
            problems=updated["restore_drill"]["problems"],
            cleanup_ok=cleanup_ok,
        )
        if not ok:
            self._notify({"event": "restore.failed", "backup_id": backup_id})
        return updated

    # --- 内部 -------------------------------------------------------------------
    def _notify(self, event: dict[str, Any]) -> None:
        try:
            self._d.notifier.notify(event)
        except Exception as exc:  # noqa: BLE001
            self._d.audit.emit("restore.notify_failed", error=type(exc).__name__)

    def _load(self, backup_id: str) -> dict[str, Any]:
        manifests = [(r, catalog.read_manifest(r / backup_id)) for r in self._roots()]
        first = manifests[0][1]
        if first is None:
            raise DrillAborted(f"manifest not found under {manifests[0][0] / backup_id}")
        problems = validate_manifest(first)
        if problems:
            raise DrillAborted(f"manifest invalid: {problems}")
        for root, m in manifests[1:]:
            if m is None or m.get("backup_id") != backup_id or m.get("files") != first["files"]:
                raise DrillAborted(f"manifest under {root} differs from the primary copy")
        return first

    def _source(self, manifest: dict[str, Any], rel: str) -> Path:
        role = manifest["files"][rel]["root"]
        root = self._db_root if role == "db" else self._object_root
        return root / manifest["backup_id"] / rel

    def _copy_files(
        self, manifest: dict[str, Any], work: Path
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """全ファイルを作業ディレクトリへ複写しつつ sha256 を照合する。"""
        bad: list[str] = []
        copied: dict[str, str] = {}
        for rel, entry in manifest["files"].items():
            src = self._source(manifest, rel)
            if not src.is_file():
                bad.append(f"{rel}: missing")
                continue
            sha = _copy_and_hash(src, work / rel)
            copied[rel] = sha
            if sha != entry["sha256"]:
                bad.append(f"{rel}: sha256 mismatch")
            elif (work / rel).stat().st_size != entry["bytes"]:
                bad.append(f"{rel}: size mismatch")
        return (
            _check(
                not bad,
                files=len(manifest["files"]),
                problems=bad[:MAX_LISTED_PROBLEMS],
                problem_count=len(bad),
            ),
            copied,
        )

    def _docker(self, *args: str, stdin: Path | None = None) -> tuple[int, str, str]:
        res = self._d.runner.run(["docker", *args], stdin_path=stdin)
        return res.returncode, res.stdout, res.stderr

    def _restore_and_verify(
        self, manifest: dict[str, Any], work: Path, copied: dict[str, str]
    ) -> dict[str, dict[str, Any]]:
        t = self._target
        name = t.container_name
        out: dict[str, dict[str, Any]] = {}
        rc, _, err = self._docker(
            "run", "-d", "--rm", "--name", name, "--network", "none",
            "--label", "avp.restore-drill=1",
            "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", f"POSTGRES_USER={RESTORE_PG_USER}",
            "-e", f"POSTGRES_PASSWORD={secrets.token_hex(16)}",
            self._d.image,
        )  # fmt: skip
        if rc != 0:
            raise DrillAborted(f"cannot start the restore container: {err.strip()[:200]}")
        self._wait_ready(name)
        pg = PostgresAccess(self._d.runner, name, RESTORE_PG_USER)
        rc, so, _ = self._docker("exec", name, "pg_restore", "--version")
        self._versions["pg_restore"] = so.strip() if rc == 0 else "unknown"
        rc, so, _ = self._docker("--version")
        self._versions["docker"] = so.strip() if rc == 0 else "unknown"

        problems: list[str] = []
        for db in manifest["databases"]:
            target_db = restore_db_name(db)
            rc, _, err = self._docker("exec", name, "createdb", "-U", RESTORE_PG_USER, target_db)
            if rc == 0:
                rc, _, err = self._docker(
                    "exec", "-i", name, "pg_restore", "-U", RESTORE_PG_USER, "-d", target_db,
                    "--no-owner", "--no-privileges", "--exit-on-error",
                    stdin=work / manifest["databases"][db]["file"],
                )  # fmt: skip
            if rc != 0:
                problems.append(f"{db}: pg_restore failed rc={rc} {err.strip()[:150]}")
        out["databases_restored"] = _check(not problems, databases=len(manifest["databases"]),
                                           problems=problems)  # fmt: skip
        if problems:
            return out

        restored: dict[str, dict[str, int]] = {}
        for db in manifest["databases"]:
            restored[db] = pg.row_counts(restore_db_name(db))
        recorded = {db: m["rows"] for db, m in manifest["databases"].items()}
        rows = row_count_problems(recorded, restored)
        out["row_counts"] = _check(not rows, problems=rows[:MAX_LISTED_PROBLEMS],
                                   tables=sum(len(v) for v in restored.values()))  # fmt: skip

        got_rev = pg.alembic_revision(restore_db_name(APP_DATABASE))
        out["alembic_revision"] = _check(got_rev == manifest["alembic_revision"], restored=got_rev,
                                         expected=manifest["alembic_revision"])  # fmt: skip

        objects: dict[tuple[str, str], str] = {}
        obj_bad = []
        for rel, entry in manifest["files"].items():
            parts = rel.split("/")
            if parts[0] != "minio" or len(parts) < 3:
                continue
            objects[(parts[1], "/".join(parts[2:]))] = copied.get(rel, "")
            if copied.get(rel) != entry["sha256"]:
                obj_bad.append(rel)
        out["object_checksums"] = _check(not obj_bad, objects=len(objects), problems=obj_bad[:20])

        refs: list[tuple[str, str, str]] = []
        for table in ARTIFACT_REFERENCE_TABLES:
            for bucket, key, sha in pg.query_rows(
                restore_db_name(APP_DATABASE),
                f"SELECT bucket, object_key, sha256 FROM {table}",  # noqa: S608 - 定数の表名
            ):
                refs.append((bucket, key, sha))
        ref_problems = reference_problems(refs, objects, set(manifest["objects"]["buckets"]))
        out["reference_integrity"] = _check(not ref_problems, rows_checked=len(refs),
                                            problems=ref_problems[:MAX_LISTED_PROBLEMS],
                                            problem_count=len(ref_problems))  # fmt: skip
        return out

    def _wait_ready(self, name: str) -> None:
        for _ in range(READY_ATTEMPTS):
            rc, _, _ = self._docker("exec", name, "pg_isready", "-U", RESTORE_PG_USER)
            if rc == 0:
                return
            self._d.sleep(1.0)
        raise DrillAborted("restore container did not become ready")

    def _cleanup(self, work: Path, container_started: bool) -> bool:
        ok = True
        name = self._target.container_name
        # 起動に失敗した場合も、同名の取り残しがあれば消す（名前は拒否判定を通った復旧先のものだけ）
        rc, _, _ = self._docker("rm", "-f", name)
        if rc != 0 and container_started:
            ok = False
        try:
            shutil.rmtree(work)
        except OSError:
            ok = False
        if not ok:
            self._d.audit.emit("restore.cleanup_failed", container=name, work_dir=str(work))
        return ok
