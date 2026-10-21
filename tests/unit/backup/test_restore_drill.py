"""restore drill: 隔離先への復旧と検証。理由は docs/testing/ の rationale。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from domain.backup.guards import ProductionTargetRefused, RestoreTarget
from domain.backup.manifest import validate_manifest
from infrastructure.backup import catalog
from infrastructure.backup.audit import AuditLog
from infrastructure.backup.restore import DrillAborted, DrillDeps, RestoreDrill
from tests.unit.backup.fakes import Clock, Harness, Notes, RestoreRunner, make_backup, overwrite


def rows_for(h: Harness) -> list[tuple[str, str, str]]:
    return [
        ("artifacts", key, hashlib.sha256(body).hexdigest())
        for key, body in h.objects.store["artifacts"].items()
    ]


def target_for(tmp_path: Path, **over: Any) -> RestoreTarget:
    base = RestoreTarget(
        container_name="avp-restore-test",
        compose_project="avp-restore-test",
        pg_host="docker-exec",
        pg_port=0,
        databases=("restore_avp", "restore_temporal", "restore_temporal_visibility"),
        buckets=("restore-artifacts",),
        work_dir=str(tmp_path / "drill-work"),
    )
    return replace(base, **over)


def drill_for(
    h: Harness, tmp_path: Path, runner: RestoreRunner, notes: Notes | None = None, **over: Any
) -> tuple[RestoreDrill, Notes, list[str]]:
    notes = notes or Notes()
    lines: list[str] = []
    deps = DrillDeps(
        runner=runner,  # type: ignore[arg-type]
        audit=AuditLog(None, write=lines.append, now=Clock()),
        notifier=notes,
        sleep=lambda s: None,
    )
    return RestoreDrill(h.db_root, h.object_root, target_for(tmp_path, **over), deps), notes, lines


def _make(tmp_path: Path) -> tuple[Harness, dict[str, Any]]:
    (tmp_path / "b").mkdir()
    h = make_backup(tmp_path / "b")
    return h, h.service.run()


def read(h: Harness, backup_id: str) -> dict[str, Any]:
    m = catalog.read_manifest(h.db_root / backup_id)
    assert m is not None
    return m


def test_a_good_backup_passes_the_drill_and_only_then_is_verified(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    assert m["restore_verified"] is False
    runner = RestoreRunner(artifact_rows=rows_for(h))
    drill, notes, lines = drill_for(h, tmp_path, runner)
    out = drill.run(m["backup_id"])
    assert out["restore_verified"] is True and out["restore_drill"]["result"] == "pass"
    for root in (h.db_root, h.object_root):  # 両 root の manifest に証跡が書かれる
        got = catalog.read_manifest(root / m["backup_id"])
        assert got is not None and got["restore_verified"] is True
        assert validate_manifest(got) == []
        assert set(got["restore_drill"]["checks"]) >= {"reference_integrity", "row_counts"}
    assert out["restore_drill"]["checks"]["reference_integrity"]["detail"]["rows_checked"] == 2
    assert out["restore_drill"]["target"]["network"] == "none"
    assert notes.events == []
    # 片付け: コンテナは消え、作業ディレクトリは無い
    assert runner.removed == ["avp-restore-test"] and runner.running == set()
    assert not (tmp_path / "drill-work").exists()
    # backup 時の内容は変わらない
    assert out["files"] == m["files"] and out["databases"] == m["databases"]


def test_the_drill_waits_for_postgres_to_become_ready(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h), ready_after=3)
    out = drill_for(h, tmp_path, runner)[0].run(m["backup_id"])
    assert out["restore_verified"] is True


def test_a_corrupted_backup_file_is_detected_and_nothing_is_restored(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    victim = h.object_root / m["backup_id"] / "minio/artifacts/a/one.json"
    overwrite(victim, b'{"a":9}')  # bit-rot / 改ざん（長さは同じ）
    runner = RestoreRunner(artifact_rows=rows_for(h))
    drill, notes, lines = drill_for(h, tmp_path, runner)
    out = drill.run(m["backup_id"])
    assert out["restore_verified"] is False and out["restore_drill"]["result"] == "fail"
    detail = out["restore_drill"]["checks"]["files_at_rest_checksums"]["detail"]
    assert detail["problems"] == ["minio/artifacts/a/one.json: sha256 mismatch"]
    assert not any("pg_restore" in c for c in runner.calls)  # 壊れた写しからは戻さない
    assert [e["event"] for e in notes.events] == ["restore.failed"]
    assert not (tmp_path / "drill-work").exists()


def test_a_missing_file_is_a_failure(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    (h.object_root / m["backup_id"] / "minio/artifacts/media/v.mp4").unlink()
    out = drill_for(h, tmp_path, RestoreRunner(artifact_rows=rows_for(h)))[0].run(m["backup_id"])
    assert out["restore_verified"] is False


def test_a_failed_drill_revokes_a_previous_verification(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h))
    assert drill_for(h, tmp_path, runner)[0].run(m["backup_id"])["restore_verified"] is True
    overwrite(h.db_root / m["backup_id"] / "pg" / "avp.dump", b"rotten")
    out = drill_for(h, tmp_path, RestoreRunner())[0].run(m["backup_id"])
    assert out["restore_verified"] is False
    assert read(h, m["backup_id"])["restore_verified"] is False


def test_row_loss_is_detected(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h), drop_rows={"public.t": 1})
    out = drill_for(h, tmp_path, runner)[0].run(m["backup_id"])
    assert out["restore_verified"] is False
    assert out["restore_drill"]["checks"]["row_counts"]["passed"] is False
    assert out["restore_drill"]["checks"]["alembic_revision"]["passed"] is True


def test_alembic_revision_mismatch_is_detected(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h), alembic="0009")
    out = drill_for(h, tmp_path, runner)[0].run(m["backup_id"])
    assert out["restore_drill"]["checks"]["alembic_revision"]["passed"] is False
    assert out["restore_verified"] is False


def test_a_db_row_pointing_at_a_missing_or_different_object_fails_integrity(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    rows = [*rows_for(h), ("artifacts", "gone/key.json", "f" * 64)]
    out = drill_for(h, tmp_path, RestoreRunner(artifact_rows=rows))[0].run(m["backup_id"])
    ref = out["restore_drill"]["checks"]["reference_integrity"]
    assert (
        ref["passed"] is False
        and "artifacts/gone/key.json: object missing" in ref["detail"]["problems"]
    )

    (tmp_path / "c").mkdir()
    h2, m2 = _make(tmp_path / "c")
    bad = [("artifacts", "a/one.json", "0" * 64)]
    out = drill_for(h2, tmp_path / "c", RestoreRunner(artifact_rows=bad))[0].run(m2["backup_id"])
    assert any(
        "sha256 differs" in p
        for p in out["restore_drill"]["checks"]["reference_integrity"]["detail"]["problems"]
    )


def test_a_pg_restore_failure_is_a_failure_and_still_cleans_up(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h), fail_restore={"temporal"})
    out = drill_for(h, tmp_path, runner)[0].run(m["backup_id"])
    assert out["restore_verified"] is False
    assert out["restore_drill"]["checks"]["databases_restored"]["passed"] is False
    assert runner.removed == ["avp-restore-test"] and not (tmp_path / "drill-work").exists()


def test_a_container_that_cannot_start_is_recorded_as_a_failed_drill(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    out = drill_for(h, tmp_path, RestoreRunner(run_rc=125))[0].run(m["backup_id"])
    assert out["restore_verified"] is False
    assert "drill_error" in out["restore_drill"]["checks"]


@pytest.mark.parametrize(
    "over",
    [
        {"container_name": "avp2-postgres-1"},
        {"compose_project": "avp2"},
        {"pg_host": "postgres"},
        {"pg_port": 5432},
        {"databases": ("avp", "temporal", "temporal_visibility")},
        {"buckets": ("artifacts",)},
        {"work_dir": "/mnt/minio-hdd/minio-data/drill"},
    ],
)
def test_restore_refuses_a_production_target_before_running_anything(
    tmp_path: Path, over: dict[str, Any]
) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner(artifact_rows=rows_for(h))
    drill = drill_for(h, tmp_path, runner, **over)[0]
    with pytest.raises(ProductionTargetRefused):
        drill.run(m["backup_id"])
    assert runner.calls == []  # docker を 1 度も呼ばない
    assert read(h, m["backup_id"])["restore_verified"] is False  # manifest も触らない
    assert not (tmp_path / "drill-work").exists()


def test_restore_refuses_to_write_into_the_backup_roots(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    runner = RestoreRunner()
    drill = drill_for(h, tmp_path, runner, work_dir=str(h.db_root / "scratch"))[0]
    with pytest.raises(ProductionTargetRefused):
        drill.run(m["backup_id"])
    assert runner.calls == []


def test_unknown_or_inconsistent_manifests_abort_without_evidence(tmp_path: Path) -> None:
    h, m = _make(tmp_path)
    with pytest.raises(DrillAborted, match="not found"):
        drill_for(h, tmp_path, RestoreRunner())[0].run("20200101T000000Z")
    other = read(h, m["backup_id"])
    other["files"]["pg/avp.dump"]["sha256"] = "b" * 64
    catalog.write_manifest(h.object_root / m["backup_id"], other)
    runner = RestoreRunner()
    with pytest.raises(DrillAborted, match="differs"):
        drill_for(h, tmp_path, runner)[0].run(m["backup_id"])
    assert runner.calls == []


def test_only_the_drill_can_set_restore_verified_in_a_manifest_on_disk(tmp_path: Path) -> None:
    """backup が確定させた manifest は常に false。drill を通すまで true にならない。"""
    h, m = _make(tmp_path)
    for root in (h.db_root, h.object_root):
        assert read(h, m["backup_id"])["restore_verified"] is False
        assert catalog.catalog(root)[0].restore_verified is False
