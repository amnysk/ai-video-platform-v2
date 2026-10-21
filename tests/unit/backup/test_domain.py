"""backup の純粋な判定（ADR-0051 / INV-66）。理由: docs/testing/backup-restore-rationale.md。"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest

from contracts.backup import BACKUP_DATABASES
from domain.backup.guards import (
    ProductionTargetRefused,
    RestoreTarget,
    assert_isolated,
    destination_problems,
    safe_relative_key,
    target_problems,
)
from domain.backup.manifest import (
    REQUIRED_DRILL_CHECKS,
    DrillEvidence,
    DrillRejected,
    apply_drill,
    new_manifest,
    reference_problems,
    row_count_problems,
    validate_manifest,
)
from domain.backup.retention import CatalogEntry, Policy, plan_retention

PROTECTED = ["/mnt/minio-hdd/minio-data", "/mnt/minio-hdd/ai-video-work"]
T0 = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)
SHA = "a" * 64


def make_manifest(backup_id: str = "20261021T030000Z") -> dict:
    dbs = {
        name: {
            "file": f"pg/{name}.dump",
            "sha256": SHA,
            "bytes": 10,
            "finished_at": (T0 + timedelta(minutes=i)).isoformat(),
            "rows": {"t": {"before": 1, "after": 2}},
        }
        for i, name in enumerate(BACKUP_DATABASES)
    }
    return new_manifest(
        backup_id=backup_id,
        started_at=T0,
        finished_at=T0 + timedelta(minutes=9),
        git_revision="g" * 40,
        git_dirty=False,
        alembic_revision="0015",
        tool_versions={"pg_dump": "17.11"},
        databases=dbs,
        objects={"finished_at": T0.isoformat(), "buckets": {"artifacts": {"count": 1, "bytes": 3}}},
        files={"pg/avp.dump": {"sha256": SHA, "bytes": 10}},
        consistency={"order": "postgres_then_minio"},
        destination={"same_device_as_source": False},
        offsite={"configured": False},
    )


def good_evidence(backup_id: str) -> DrillEvidence:
    checks = {name: {"passed": True, "detail": "ok"} for name in REQUIRED_DRILL_CHECKS}
    return DrillEvidence(backup_id, T0, {"container": "avp-restore-x"}, checks, {"docker": "1"})


# --- manifest -----------------------------------------------------------------------


def test_a_new_manifest_is_valid_and_never_restore_verified() -> None:
    m = make_manifest()
    assert validate_manifest(m) == []
    assert m["restore_verified"] is False
    assert m["restore_drill"] is None


def test_validation_rejects_missing_database_or_unverifiable_claim() -> None:
    m = make_manifest()
    del m["databases"]["temporal"]
    assert any("databases must be exactly" in p for p in validate_manifest(m))
    m = make_manifest()
    m["restore_verified"] = True  # 証跡なしの自己申告
    assert any("without restore_drill" in p for p in validate_manifest(m))
    m = make_manifest()
    m["files"]["pg/avp.dump"]["sha256"] = "zz"
    assert any("without sha256" in p for p in validate_manifest(m))
    assert validate_manifest("x") == ["manifest is not an object"]


def test_only_a_fully_passing_drill_flips_restore_verified() -> None:
    m = make_manifest()
    out = apply_drill(m, good_evidence(m["backup_id"]))
    assert out["restore_verified"] is True
    assert out["restore_drill"]["result"] == "pass"
    assert validate_manifest(out) == []
    # backup 時の内容は変えない
    keep = {k: v for k, v in out.items() if k not in {"restore_verified", "restore_drill"}}
    assert keep == {k: v for k, v in m.items() if k not in {"restore_verified", "restore_drill"}}
    assert m["restore_verified"] is False  # 入力は破壊しない


@pytest.mark.parametrize("which", REQUIRED_DRILL_CHECKS)
def test_a_failed_or_missing_check_keeps_the_backup_unverified(which: str) -> None:
    m = make_manifest()
    ev = good_evidence(m["backup_id"])
    failed = copy.deepcopy(dict(ev.checks))
    failed[which] = {"passed": False, "detail": "boom"}
    out = apply_drill(m, DrillEvidence(ev.backup_id, ev.at, ev.target, failed, ev.tool_versions))
    assert out["restore_verified"] is False and out["restore_drill"]["result"] == "fail"
    missing = {k: v for k, v in ev.checks.items() if k != which}
    out = apply_drill(m, DrillEvidence(ev.backup_id, ev.at, ev.target, missing, ev.tool_versions))
    assert out["restore_verified"] is False


def test_evidence_for_another_backup_is_rejected() -> None:
    m = make_manifest()
    with pytest.raises(DrillRejected):
        apply_drill(m, good_evidence("20250101T000000Z"))


def test_row_counts_must_fall_inside_the_backup_window() -> None:
    rec = {"avp": {"t": {"before": 5, "after": 7}}}
    assert row_count_problems(rec, {"avp": {"t": 6}}) == []
    assert row_count_problems(rec, {"avp": {"t": 4}})
    assert row_count_problems(rec, {"avp": {"t": 8}})
    assert row_count_problems(rec, {"avp": {}})  # 表が消えた
    assert row_count_problems(rec, {"avp": {"t": 5, "x": 1}})  # 余計な表
    assert row_count_problems(rec, {})  # DB が復旧されていない


def test_reference_integrity_flags_missing_object_bad_sha_and_unbacked_bucket() -> None:
    rows = [("artifacts", "k1", "s1"), ("artifacts", "k2", "s2"), ("artifacts", "k3", "s3")]
    have = {("artifacts", "k1"): "s1", ("artifacts", "k2"): "WRONG"}
    problems = reference_problems(rows, have, {"artifacts"})
    assert len(problems) == 2
    assert any("k2" in p and "sha256" in p for p in problems)
    assert any("k3" in p and "missing" in p for p in problems)
    assert reference_problems([("other", "k", "s")], {}, {"artifacts"})
    assert reference_problems(rows[:1], have, {"artifacts"}) == []


# --- restore target guard -----------------------------------------------------------


def isolated(**over: object) -> RestoreTarget:
    base = {
        "container_name": "avp-restore-1",
        "compose_project": "avp-restore-1",
        "pg_host": "127.0.0.1",
        "pg_port": 55432,
        "databases": ("restore_avp", "restore_temporal"),
        "buckets": ("restore-artifacts",),
        "work_dir": "/var/tmp/avp-restore-1",
    }
    base.update(over)
    return RestoreTarget(**base)  # type: ignore[arg-type]


def test_an_isolated_target_is_accepted() -> None:
    assert target_problems(isolated()) == []
    assert_isolated(isolated())


@pytest.mark.parametrize(
    "over",
    [
        {"compose_project": "avp2"},
        {"container_name": "avp2-postgres-1"},
        {"container_name": "avp2-minio-1"},
        {"container_name": "some-other"},
        {"pg_host": "postgres"},
        {"pg_host": "minio"},
        {"pg_host": "localhost", "pg_port": 5432},
        {"pg_port": 5432},
        {"pg_port": 9000},
        {"pg_port": 7233},
        {"databases": ("avp",)},
        {"databases": ("restore_avp", "temporal")},
        {"databases": ("avp_test",)},
        {"databases": ()},
        {"buckets": ("artifacts",)},
        {"buckets": ()},
        {"work_dir": "/mnt/minio-hdd/minio-data"},
        {"work_dir": "/mnt/minio-hdd/minio-data/restore"},
        {"work_dir": "/mnt/minio-hdd/ai-video-work/../minio-data"},
        {"work_dir": "relative/dir"},
    ],
)
def test_restore_refuses_any_production_identifier(over: dict) -> None:
    target = isolated(**over)
    assert target_problems(target, PROTECTED)
    with pytest.raises(ProductionTargetRefused):
        assert_isolated(target, PROTECTED)


def test_restore_refuses_the_backup_root_and_reports_every_reason() -> None:
    t = isolated(work_dir="/srv/backups/db/x", pg_port=5432, compose_project="avp2")
    problems = target_problems(t, protected_paths=["/srv/backups"])
    assert len(problems) == 3  # project / port / protected path


def test_destination_on_the_same_device_is_a_problem_unless_explicitly_allowed() -> None:
    assert destination_problems("db", 1, 1, allow_same_device=False)
    assert destination_problems("db", 1, 2, allow_same_device=False) == []
    assert destination_problems("db", 1, 1, allow_same_device=True) == []


@pytest.mark.parametrize("key", ["", "/abs", "a/../b", "a//b", "a/./b", "..", "a\\b", "a\x00b"])
def test_unsafe_object_keys_are_not_turned_into_paths(key: str) -> None:
    assert safe_relative_key("minio/artifacts", key) is None


def test_safe_object_key_becomes_a_relative_path() -> None:
    assert safe_relative_key("minio/artifacts", "a/b.json") == "minio/artifacts/a/b.json"


# --- retention ----------------------------------------------------------------------


def entry(days_ago: int, *, verified: bool = True, complete: bool = True, hour: int = 3):
    at = (T0 - timedelta(days=days_ago)).replace(hour=hour)
    return CatalogEntry(at.strftime("%Y%m%dT%H%M%SZ"), at, complete, verified)


def test_retention_never_deletes_unverified_or_incomplete() -> None:
    entries = [entry(d, verified=False) for d in range(0, 60)] + [entry(70, complete=False)]
    plan = plan_retention(entries, Policy(1, 0, 0))
    assert plan.delete == []
    assert any("no restore-verified" in w for w in plan.warnings)


def test_retention_never_deletes_the_last_verified_backup() -> None:
    old = entry(100)  # 唯一の検証済み。方針では選ばれない
    newer_unverified = [entry(d, verified=False) for d in range(0, 10)]
    plan = plan_retention([old, *newer_unverified], Policy(2, 0, 0))
    assert old.backup_id not in plan.delete
    assert "last-verified" in plan.keep[old.backup_id]


def test_retention_deletes_verified_backups_outside_the_policy_only() -> None:
    entries = [entry(d) for d in range(0, 20)]
    plan = plan_retention(entries, Policy(keep_daily=3, keep_weekly=0, keep_monthly=0))
    kept = sorted(plan.keep)
    assert kept == sorted(e.backup_id for e in entries[:3])
    assert len(plan.delete) == 17
    assert set(plan.delete).isdisjoint(plan.keep)


def test_retention_keeps_the_newest_of_each_day_week_and_month() -> None:
    two_same_day = [entry(0, hour=1), entry(0, hour=5)]
    plan = plan_retention(two_same_day, Policy(1, 0, 0))
    assert plan.keep.keys() == {two_same_day[1].backup_id}
    assert plan.delete == [two_same_day[0].backup_id]
    weekly = plan_retention([entry(d) for d in range(0, 70)], Policy(0, 4, 0))
    assert len([k for k, v in weekly.keep.items() if "weekly" in v]) == 4
    monthly = plan_retention([entry(d) for d in range(0, 200)], Policy(0, 0, 3))
    assert len([k for k, v in monthly.keep.items() if "monthly" in v]) == 3
