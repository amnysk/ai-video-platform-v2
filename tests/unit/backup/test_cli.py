"""scripts/backup.py の終了コードと「本番に触れない」性質。理由は docs/testing/ の rationale。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from contracts.backup import EXIT_FAILED, EXIT_OK, EXIT_REFUSED
from infrastructure.backup import catalog
from infrastructure.backup.cli import main
from infrastructure.backup.runner import CmdResult, FileResult
from tests.unit.backup.fakes import T0, Clock, make_backup, overwrite


class Refusing:
    """全ての外部コマンドを記録して失敗させる。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv: Sequence[str], **_: object) -> CmdResult:
        self.calls.append(list(argv))
        return CmdResult(1, "", "no docker here")

    def run_to_file(self, argv: Sequence[str], dest: Path, **_: object) -> FileResult:
        raise AssertionError("must not dump")


def env_for(tmp_path: Path) -> dict[str, str]:
    return {
        "AVP_BACKUP_DB_ROOT": str(tmp_path / "db"),
        "AVP_BACKUP_OBJECT_ROOT": str(tmp_path / "obj"),
    }


def test_there_is_no_default_destination(tmp_path: Path) -> None:
    r = Refusing()
    assert main(["run"], {}, runner=r) == EXIT_REFUSED  # type: ignore[arg-type]
    assert r.calls == []


def test_a_missing_root_directory_is_a_usage_error(tmp_path: Path) -> None:
    r = Refusing()
    assert main(["status"], env_for(tmp_path), runner=r) == EXIT_REFUSED  # type: ignore[arg-type]


def test_run_fails_closed_when_it_cannot_prove_the_destination_is_separate(tmp_path: Path) -> None:
    make_backup(tmp_path)  # root を作る
    r = Refusing()
    code = main(["--env-file", str(tmp_path / "none.env"), "run"], env_for(tmp_path), runner=r)  # type: ignore[arg-type]
    assert code == EXIT_FAILED
    assert all(c[:2] in (["docker", "inspect"], ["docker", "--version"]) for c in r.calls)
    assert catalog.list_backup_ids(tmp_path / "db") == []
    audit = (tmp_path / "db" / "audit.log").read_text().splitlines()
    assert "backup.failed" in [json.loads(x)["event"] for x in audit]


def test_restore_drill_refuses_a_production_work_root_before_any_docker_call(
    tmp_path: Path,
) -> None:
    h = make_backup(tmp_path)
    m = h.service.run()
    r = Refusing()
    code = main(
        [
            "restore-drill",
            "--backup-id",
            m["backup_id"],
            "--work-root",
            "/mnt/minio-hdd/minio-data",
        ],
        env_for(tmp_path),
        runner=r,  # type: ignore[arg-type]
    )
    assert code == EXIT_REFUSED
    assert r.calls == []
    assert catalog.read_manifest(tmp_path / "db" / m["backup_id"])["restore_verified"] is False  # type: ignore[index]


def test_restore_drill_refuses_a_work_root_inside_the_backup_roots(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    m = h.service.run()
    r = Refusing()
    code = main(
        ["restore-drill", "--backup-id", "latest", "--work-root", str(tmp_path / "db")],
        env_for(tmp_path),
        runner=r,  # type: ignore[arg-type]
    )
    assert code == EXIT_REFUSED and r.calls == [] and m


def test_restore_drill_for_an_unknown_backup_is_a_usage_error(tmp_path: Path) -> None:
    make_backup(tmp_path)
    r = Refusing()
    assert main(["restore-drill", "--backup-id", "latest"], env_for(tmp_path), runner=r) == (  # type: ignore[arg-type]
        EXIT_REFUSED
    )
    assert r.calls == []


def test_verify_finds_bit_rot_and_extra_files(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    m = h.service.run()
    r = Refusing()
    argv = ["verify", "--backup-id", m["backup_id"]]
    assert main(argv, env_for(tmp_path), runner=r) == EXIT_OK  # type: ignore[arg-type]
    overwrite((tmp_path / "obj" / m["backup_id"] / "minio/artifacts/a/one.json"), b"rot")
    assert main(argv, env_for(tmp_path), runner=r) == EXIT_FAILED  # type: ignore[arg-type]
    overwrite((tmp_path / "obj" / m["backup_id"] / "minio/artifacts/a/one.json"), b'{"a":1}')
    (tmp_path / "obj" / m["backup_id"] / "stray.bin").write_bytes(b"x")
    assert main(argv, env_for(tmp_path), runner=r) == EXIT_FAILED  # type: ignore[arg-type]


def test_prune_is_a_dry_run_unless_applied_and_status_reports_staleness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    h = make_backup(tmp_path, clock=Clock())
    m = h.service.run()
    r = Refusing()
    assert main(["prune"], env_for(tmp_path), runner=r) == EXIT_OK  # type: ignore[arg-type]
    assert json.loads(capsys.readouterr().out)["delete"] == []
    assert catalog.list_backup_ids(tmp_path / "db") == [m["backup_id"]]
    assert main(["status", "--max-age-hours", "100000"], env_for(tmp_path), runner=r) == EXIT_OK  # type: ignore[arg-type]
    capsys.readouterr()
    later = T0 + timedelta(days=3)
    code = main(["status", "--max-age-hours", "26"], env_for(tmp_path), runner=r, now=lambda: later)  # type: ignore[arg-type]
    assert code == EXIT_FAILED
