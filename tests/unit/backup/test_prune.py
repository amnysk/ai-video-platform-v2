"""保持の実行（ディスク上の集合）。検証済みでないものは消えない。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from domain.backup.retention import Policy
from infrastructure.backup import catalog
from infrastructure.backup import prune as prune_mod
from infrastructure.backup.audit import AuditLog
from infrastructure.backup.prune import prune
from tests.unit.backup.fakes import T0, Clock, RestoreRunner, make_backup
from tests.unit.backup.test_restore_drill import drill_for, rows_for


def audit(lines: list[str]) -> AuditLog:
    return AuditLog(None, write=lines.append, now=Clock())


def make_sets(tmp_path: Path, days: int) -> list[str]:
    """``days`` 日分の backup を作る（1 日 1 つ）。時計だけを進める。"""
    ids = []
    clock = Clock()
    for d in range(days):
        clock.t = T0 + timedelta(days=d)
        sub = tmp_path
        h = make_backup(sub, clock=clock)
        ids.append(h.service.run()["backup_id"])
    return ids


def verify(tmp_path: Path, backup_id: str) -> None:
    h = make_backup(tmp_path)  # 同じ root を指す Harness（rows_for のため）
    runner = RestoreRunner(artifact_rows=rows_for(h))
    drill_for(h, tmp_path / "w", runner)[0].run(backup_id)


def test_unverified_backups_are_never_pruned_even_far_outside_the_policy(tmp_path: Path) -> None:
    ids = make_sets(tmp_path, 5)
    lines: list[str] = []
    plan = _prune(tmp_path, lines, dry_run=False)
    assert plan.delete == []
    assert sorted(catalog.list_backup_ids(tmp_path / "db")) == sorted(ids)
    assert any("no restore-verified" in json.loads(x)["warning"] for x in lines if "warning" in x)


def _prune(tmp_path: Path, lines: list[str], *, dry_run: bool, daily: int = 1):
    return prune(
        tmp_path / "db", tmp_path / "obj", Policy(daily, 0, 0), audit(lines), dry_run=dry_run
    )


def test_verified_old_backups_are_deleted_from_both_roots_but_the_newest_stays(
    tmp_path: Path,
) -> None:
    ids = make_sets(tmp_path, 4)
    for i in ids:
        verify(tmp_path, i)
    lines: list[str] = []
    dry = _prune(tmp_path, lines, dry_run=True)
    assert dry.delete == sorted(ids[:3])
    assert sorted(catalog.list_backup_ids(tmp_path / "db")) == sorted(ids)  # dry-run は消さない
    _prune(tmp_path, lines, dry_run=False)
    for root in ("db", "obj"):
        assert catalog.list_backup_ids(tmp_path / root) == [ids[3]]


def test_the_last_verified_backup_survives_even_when_newer_ones_are_unverified(
    tmp_path: Path,
) -> None:
    ids = make_sets(tmp_path, 4)
    verify(tmp_path, ids[0])  # 検証済みは最古の 1 つだけ
    _prune(tmp_path, [], dry_run=False)
    assert sorted(catalog.list_backup_ids(tmp_path / "db")) == sorted(ids)


def test_a_manifest_that_lost_its_verification_between_plan_and_delete_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = make_sets(tmp_path, 3)
    for i in ids:
        verify(tmp_path, i)
    real = prune_mod._still_verified

    def flaky(root: Path, backup_id: str) -> bool:
        if backup_id == ids[0]:
            return False  # 計画後に検証が取り消された（例: 再 drill が失敗）
        return real(root, backup_id)

    monkeypatch.setattr(prune_mod, "_still_verified", flaky)
    lines: list[str] = []
    _prune(tmp_path, lines, dry_run=False)
    assert ids[0] in catalog.list_backup_ids(tmp_path / "db")
    assert ids[1] not in catalog.list_backup_ids(tmp_path / "db")
    assert any("not verified on re-read" in x for x in lines)


def test_verification_in_only_one_root_does_not_count(tmp_path: Path) -> None:
    ids = make_sets(tmp_path, 3)
    for i in ids:
        verify(tmp_path, i)
    broken = catalog.read_manifest(tmp_path / "obj" / ids[0])
    assert broken is not None
    broken["restore_verified"] = False
    catalog.write_manifest(tmp_path / "obj" / ids[0], broken)
    _prune(tmp_path, [], dry_run=False)
    assert ids[0] in catalog.list_backup_ids(tmp_path / "db")


def test_delete_only_touches_backup_id_directories(tmp_path: Path) -> None:
    (tmp_path / "keepme").mkdir()
    (tmp_path / "20260101T000000Z.partial").mkdir()
    with pytest.raises(ValueError):
        catalog.delete_backup_set(tmp_path, "keepme")
    with pytest.raises(ValueError):
        catalog.delete_backup_set(tmp_path, "20260101T000000Z.partial")
    link = tmp_path / "20260101T000000Z"
    link.symlink_to(tmp_path / "keepme", target_is_directory=True)
    assert catalog.delete_backup_set(tmp_path, "20260101T000000Z") is False
    assert (tmp_path / "keepme").is_dir()
