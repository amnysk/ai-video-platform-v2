"""precheck が read-only で、各項目を PASS/FAIL/UNKNOWN に落とすこと。

理由: docs/testing/backup-restore-rationale.md。docker・実 MinIO・実ディスクには触れない。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from contracts.backup import EXIT_FAILED
from infrastructure.backup.cli import main
from infrastructure.backup.postgres import PostgresAccess
from infrastructure.backup.precheck import PrecheckConfig, PrecheckDeps, Prechecker
from infrastructure.backup.runner import CmdResult
from tests.unit.backup.fakes import FakeObjects

SECRET = "s3cr3t-value-must-never-appear"
NOW = datetime(2026, 10, 21, 4, 5, tzinfo=UTC)  # daily(21:00 UTC)・watchdog(:35) から離れている


@dataclass
class PreRunner:
    sizes: dict[str, int] = field(
        default_factory=lambda: {
            "avp": 10 * 2**20,
            "temporal": 5 * 2**20,
            "temporal_visibility": 2**20,
        }
    )
    server_num: str = "170011"
    dump_version: str = "pg_dump (PostgreSQL) 17.11"
    alembic: str = "0016"
    activity: list[list[str]] = field(default_factory=list)
    locks: str = "0"
    pg_down: bool = False
    env: list[str] = field(
        default_factory=lambda: [f"MINIO_ROOT_PASSWORD={SECRET}", "MINIO_ROOT_USER=x"]
    )
    mounts_ok: bool = True
    calls: list[list[str]] = field(default_factory=list)

    def run(self, argv: Sequence[str], **_: object) -> CmdResult:
        a = list(argv)
        self.calls.append(a)
        if a[:2] == ["docker", "inspect"]:
            tmpl = a[3]
            if "Mounts" in tmpl:
                if not self.mounts_ok:
                    return CmdResult(1, "", "no such container")
                dest = "pg" if "postgres" in a[-1] else "minio"
                src = {"pg": "/pgdata", "minio": "/mnt/minio-hdd/minio-data"}[dest]
                d = "/var/lib/postgresql/data" if dest == "pg" else "/data"
                return CmdResult(0, json.dumps([{"Destination": d, "Source": src}]), "")
            return CmdResult(0, json.dumps(self.env), "")
        if self.pg_down:
            return CmdResult(1, "", "container is not running")
        if "--version" in a:
            return CmdResult(0, self.dump_version + "\n", "")
        sql = a[-1]
        sep = "\x1f"
        if "pg_database_size" in sql:
            return CmdResult(0, "\n".join(f"{k}{sep}{v}" for k, v in self.sizes.items()), "")
        if "server_version_num" in sql:
            return CmdResult(0, self.server_num, "")
        if "alembic_version" in sql:
            return CmdResult(0, self.alembic, "")
        if "pg_stat_activity" in sql:
            return CmdResult(0, "\n".join(sep.join(r) for r in self.activity), "")
        if "pg_locks" in sql:
            return CmdResult(0, self.locks, "")
        raise AssertionError(f"unexpected {a}")

    def run_to_file(self, *a: object, **k: object) -> Any:
        raise AssertionError("precheck must never dump")


@dataclass
class FakeFs:
    existing: set[str] = field(default_factory=lambda: {"/pgdata", "/mnt/minio-hdd/minio-data"})
    devices: dict[str, int] = field(
        default_factory=lambda: {
            "/pgdata": 1,
            "/mnt/minio-hdd/minio-data": 2,
            "/mnt/minio-hdd/avp-backups": 2,
            "/home/u/avp-backups": 1,
        }
    )
    free: dict[str, int] = field(default_factory=dict)
    not_writable: set[str] = field(default_factory=set)

    def exists(self, path: str) -> bool:
        return path in self.existing

    def device(self, path: str) -> int:
        return self.devices[path]

    def free_bytes(self, path: str) -> int:
        return self.free.get(path, 10**12)

    def writable(self, path: str) -> bool:
        return path not in self.not_writable


def build(
    runner: PreRunner | None = None,
    fs: FakeFs | None = None,
    objects: FakeObjects | None = None,
    *,
    db_root: str | None = "/mnt/minio-hdd/avp-backups",
    object_root: str | None = "/home/u/avp-backups",
    head: str = "0016",
    cred_sources: dict[str, str] | None = None,
) -> tuple[Prechecker, PreRunner, FakeFs]:
    runner = runner or PreRunner()
    fs = fs or FakeFs()
    cfg = PrecheckConfig(
        db_root=db_root,
        object_root=object_root,
        buckets=("artifacts",),
        postgres_container="avp2-postgres-1",
        minio_container="avp2-minio-1",
        protected_paths=("/mnt/minio-hdd/minio-data", "/mnt/minio-hdd/ai-video-work"),
        daily_cron="0 6 * * *",
        schedule_timezone="Asia/Tokyo",
        watchdog_cron="35 * * * *",
        credential_sources=cred_sources
        or {
            "MINIO_ACCESS_KEY": "env-file",
            "MINIO_SECRET_KEY": "env-file",
            "POSTGRES_USER": "built-in default",
        },
    )
    deps = PrecheckDeps(
        runner=runner,
        postgres=PostgresAccess(runner, "avp2-postgres-1", "avp"),
        objects=objects or FakeObjects(),
        fs=fs,
        now=lambda: NOW,
        repo_alembic_head=lambda: head,
    )
    return Prechecker(cfg, deps), runner, fs


def by_id(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {c["id"]: c for c in report["checks"]}


def test_a_healthy_system_passes_and_nothing_is_written_or_dumped() -> None:
    pc, runner, _ = build()
    report = pc.run()
    assert report["verdict"] == "PASS", report
    assert report["read_only"] is True
    # docker は inspect と exec(psql SELECT / pg_dump --version) だけ
    for a in runner.calls:
        assert a[:2] == ["docker", "inspect"] or (a[:2] == ["docker", "exec"]), a
        if a[:2] == ["docker", "exec"]:
            assert "--version" in a or a[-1].lstrip().upper().startswith("SELECT"), a


def test_secret_values_never_appear_in_the_report() -> None:
    pc, _, _ = build(runner=PreRunner(env=[f"MINIO_KMS_SECRET_KEY=k:{SECRET}", "OTHER=1"]))
    report = pc.run()
    text = json.dumps(report)
    assert SECRET not in text
    assert by_id(report)["minio_kms"]["data"]["env_names"] == ["MINIO_KMS_SECRET_KEY"]
    assert by_id(report)["minio_kms"]["status"] == "UNKNOWN"  # 平文の写しになる。所有者が判断


def test_unset_roots_fail_and_are_not_created() -> None:
    pc, _, _ = build(db_root=None, object_root=None)
    checks = by_id(pc.run())
    assert checks["root_db"]["status"] == checks["root_objects"]["status"] == "FAIL"


def test_a_missing_root_is_reported_as_would_be_created_not_created() -> None:
    pc, _, _ = build()
    data = by_id(pc.run())["root_db"]["data"]
    assert data["exists"] is False and data["would_be_created"] is True


def test_a_root_on_the_same_device_as_what_it_protects_fails() -> None:
    fs = FakeFs()
    fs.devices["/mnt/minio-hdd/avp-backups"] = 1  # pgdata と同じ
    pc, _, _ = build(fs=fs)
    assert by_id(pc.run())["root_db"]["status"] == "FAIL"


def test_a_root_inside_the_minio_data_dir_fails() -> None:
    fs = FakeFs()
    fs.devices["/mnt/minio-hdd/minio-data/backups"] = 9
    pc, _, _ = build(fs=fs, db_root="/mnt/minio-hdd/minio-data/backups")
    check = by_id(pc.run())["root_db"]
    assert check["status"] == "FAIL" and "protected" in check["detail"]


def test_a_non_writable_root_fails() -> None:
    fs = FakeFs(not_writable={"/home/u/avp-backups"})
    pc, _, _ = build(fs=fs)
    assert by_id(pc.run())["root_objects"]["status"] == "FAIL"


def test_unprovable_source_device_is_unknown_not_pass() -> None:
    pc, _, _ = build(runner=PreRunner(mounts_ok=False))
    assert by_id(pc.run())["root_db"]["status"] == "UNKNOWN"


def test_free_space_is_checked_against_sources_times_two() -> None:
    sizes = {"avp": 600, "temporal": 0, "temporal_visibility": 0}
    fs = FakeFs(free={"/mnt/minio-hdd/avp-backups": 1199})
    pc, _, _ = build(runner=PreRunner(sizes=sizes), fs=fs)
    check = by_id(pc.run())["free_space"]
    assert check["status"] == "FAIL"
    fs2 = FakeFs(free={"/mnt/minio-hdd/avp-backups": 1200})
    pc2, _, _ = build(runner=PreRunner(sizes=sizes), fs=fs2)
    assert by_id(pc2.run())["free_space"]["status"] == "PASS"


def test_roots_on_one_device_need_the_sum() -> None:
    fs = FakeFs()
    fs.devices["/home/u/avp-backups"] = 2
    fs.free = {"/mnt/minio-hdd/avp-backups": 820, "/home/u/avp-backups": 820}
    sizes = {"avp": 400, "temporal": 0, "temporal_visibility": 0}
    # db 800 と objects 24 は単独ならどちらも 820 に収まるが、同じ device なので合計 824 が要る
    pc, _, _ = build(runner=PreRunner(sizes=sizes), fs=fs)
    check = by_id(pc.run())["free_space"]
    assert check["status"] == "FAIL"
    assert check["data"]["devices"][0]["roles"] == ["db", "objects"]


def test_an_older_pg_dump_than_the_server_fails() -> None:
    pc, _, _ = build(runner=PreRunner(dump_version="pg_dump (PostgreSQL) 16.4"))
    assert by_id(pc.run())["postgres_version"]["status"] == "FAIL"


def test_a_missing_target_database_fails() -> None:
    sizes = {"avp": 1, "temporal": 1}
    pc, _, _ = build(runner=PreRunner(sizes=sizes))
    check = by_id(pc.run())["postgres_databases"]
    assert check["status"] == "FAIL" and "temporal_visibility" in check["detail"]


def test_an_unreachable_postgres_fails_and_downstream_is_unknown() -> None:
    pc, _, _ = build(runner=PreRunner(pg_down=True))
    report = pc.run()
    assert by_id(report)["postgres"]["status"] == "FAIL"
    assert by_id(report)["free_space"]["status"] == "UNKNOWN"
    assert report["verdict"] == "FAIL"


def test_a_long_running_ddl_or_exclusive_lock_fails() -> None:
    act = [["77", "active", "900", "ALTER TABLE x ADD COLUMN y int"]]
    pc, _, _ = build(runner=PreRunner(activity=act))
    assert by_id(pc.run())["postgres_activity"]["status"] == "FAIL"
    pc2, _, _ = build(runner=PreRunner(locks="2"))
    assert by_id(pc2.run())["postgres_activity"]["status"] == "FAIL"


def test_missing_bucket_fails_and_s3_errors_are_reported_by_type_only() -> None:
    pc, _, _ = build(objects=FakeObjects(store={}))
    assert by_id(pc.run())["minio_bucket"]["status"] == "FAIL"

    class Broken(FakeObjects):
        def bucket_exists(self, bucket: str) -> bool:
            raise ConnectionError(f"https://admin:{SECRET}@minio")

    report = build(objects=Broken())[0].run()
    assert by_id(report)["minio"]["status"] == "FAIL"
    assert SECRET not in json.dumps(report)


def test_object_counts_and_versioning_are_reported() -> None:
    data = by_id(build()[0].run())["minio_bucket"]["data"]
    assert data["count"] == 2 and data["bytes"] == len(b'{"a":1}') + len(b"VIDEO")
    assert data["versioning"] == {"artifacts": "unversioned"}


def test_missing_credentials_fail_and_defaults_are_called_out() -> None:
    pc, _, _ = build(cred_sources={"MINIO_ACCESS_KEY": "env-file"})
    assert by_id(pc.run())["credentials"]["status"] == "FAIL"
    check = by_id(build()[0].run())["credentials"]
    assert check["status"] == "PASS" and "built-in default" in check["detail"]


def test_alembic_mismatch_is_unknown_and_unreadable_is_fail() -> None:
    pc, _, _ = build(head="0017")
    assert by_id(pc.run())["alembic"]["status"] == "UNKNOWN"
    r = PreRunner(alembic="")
    r_checks = by_id(build(runner=r)[0].run())
    assert r_checks["alembic"]["status"] == "FAIL"


def test_starting_inside_the_daily_window_fails_with_a_next_safe_start() -> None:
    pc, _, _ = build()
    pc._d.now = lambda: datetime(2026, 10, 21, 20, 55, tzinfo=UTC)  # noqa: SLF001
    check = by_id(pc.run())["schedule_window"]
    assert check["status"] == "FAIL" and check["data"]["next_safe_start"]


def test_the_estimate_reports_duration_and_load() -> None:
    est = build()[0].run()["estimate"]
    assert est["seconds"] > 0 and "postgres" in est["load"] and "minio" in est["load"]


# --- CLI ---------------------------------------------------------------------------------


def test_the_cli_precheck_creates_nothing_even_without_roots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    runner = PreRunner(pg_down=True)
    env = {
        "AVP_BACKUP_DB_ROOT": str(tmp_path / "db"),
        "AVP_BACKUP_OBJECT_ROOT": str(tmp_path / "obj"),
    }
    envfile = tmp_path / ".env"
    envfile.write_text(f"MINIO_SECRET_KEY={SECRET}\n")
    code = main(
        ["--env-file", str(envfile), "precheck", "--json-only"],
        env,
        runner=runner,  # type: ignore[arg-type]
    )
    out = capsys.readouterr()
    assert code == EXIT_FAILED
    assert list(tmp_path.iterdir()) == [envfile]  # root も audit.log も作らない
    assert SECRET not in out.out + out.err
    report = json.loads(out.out)
    assert report["verdict"] == "FAIL" and report["read_only"] is True
    creds = [c for c in report["checks"] if c["id"] == "credentials"][0]
    assert creds["data"]["sources"]["MINIO_SECRET_KEY"] == "env-file"
