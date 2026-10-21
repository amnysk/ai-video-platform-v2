"""backup の実行（失敗経路・checksum・hardlink・offsite）。理由は docs/testing/ の rationale。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from domain.backup.manifest import validate_manifest
from infrastructure.backup import catalog
from infrastructure.backup.service import BackupFailed
from tests.unit.backup.fakes import FakeObjects, FakeRunner, Notes, make_backup


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def events(lines: list[str]) -> list[str]:
    return [json.loads(line)["event"] for line in lines]


def test_a_backup_writes_a_valid_unverified_manifest_with_every_file_hashed(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    m = h.service.run()
    assert validate_manifest(m) == []
    assert m["restore_verified"] is False
    assert m["git"] == {"revision": "1" * 40, "dirty": False}
    assert m["alembic_revision"] == "0015"
    assert m["tool_versions"]["pg_dump"].startswith("pg_dump (PostgreSQL)")
    assert m["objects"]["count"] == 2 and m["objects"]["bytes"] == 12
    assert m["databases"]["avp"]["rows"]["public.t"] == {"before": 3, "after": 3}
    assert m["consistency"]["order"] == "postgres_then_minio"
    assert m["consistency"]["postgres_finished_at"] <= m["consistency"]["minio_started_at"]
    # 全てのファイルが manifest にあり、sha256 が実体と一致する
    for rel, entry in m["files"].items():
        root = h.db_root if entry["root"] == "db" else h.object_root
        assert sha(root / m["backup_id"] / rel) == entry["sha256"], rel
    assert {
        "pg/avp.dump",
        "pg/temporal.dump",
        "pg/temporal_visibility.dump",
        "pg/globals.sql",
    } <= set(m["files"])
    assert "minio/artifacts/a/one.json" in m["files"]
    # 確定後は manifest が両 root に在り、partial は残らない
    for root in (h.db_root, h.object_root):
        assert (root / m["backup_id"] / "manifest.json").is_file()
        assert not list(root.glob("*.partial"))
    assert events(h.lines) == [
        "backup.started",
        "backup.database_dumped",
        "backup.database_dumped",
        "backup.database_dumped",
        "backup.bucket_copied",
        "backup.completed",
    ]
    assert h.notes.events == []
    assert m["offsite"] == {"configured": False, "ok": None}


def test_files_are_private_to_the_owner(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    m = h.service.run()
    d = h.db_root / m["backup_id"]
    assert (d / "pg" / "avp.dump").stat().st_mode & 0o077 == 0
    assert (d / "pg" / "globals.sql").stat().st_mode & 0o077 == 0
    # 書き込み不可: 前回と共有する hardlink の本体を誤って書き換えると全世代が壊れる
    assert (d / "pg" / "avp.dump").stat().st_mode & 0o222 == 0
    assert (
        h.object_root / m["backup_id"] / "minio/artifacts/a/one.json"
    ).stat().st_mode & 0o222 == 0
    assert d.stat().st_mode & 0o077 == 0


def test_pg_dump_failure_exits_with_audit_line_notification_and_no_backup(tmp_path: Path) -> None:
    h = make_backup(tmp_path, runner=FakeRunner(fail_dump={"temporal"}))
    with pytest.raises(BackupFailed, match="pg_dump temporal failed"):
        h.service.run()
    assert "backup.failed" in events(h.lines)
    assert [e["event"] for e in h.notes.events] == ["backup.failed"]
    assert "boom" in h.notes.events[0]["error"]
    assert list(h.db_root.iterdir()) == [] and list(h.object_root.iterdir()) == []
    assert not any(r for r in h.lines if 'restore_verified": true' in r)


def test_empty_or_unreadable_dump_is_a_failure_not_a_backup(tmp_path: Path) -> None:
    with pytest.raises(BackupFailed):
        make_backup(tmp_path, runner=FakeRunner(empty_dump={"avp"})).service.run()
    other = tmp_path / "x"
    other.mkdir()
    with pytest.raises(BackupFailed, match="unreadable"):
        make_backup(other, runner=FakeRunner(toc_rc=1)).service.run()


def test_a_migration_during_the_backup_fails_it(tmp_path: Path) -> None:
    h = make_backup(tmp_path, runner=FakeRunner(alembic=["0015", "0016"]))
    with pytest.raises(BackupFailed, match="alembic revision changed"):
        h.service.run()


def test_size_mismatch_and_etag_mismatch_are_detected(tmp_path: Path) -> None:
    for kind in ("size_lie", "etag_lie"):
        sub = tmp_path / kind
        sub.mkdir()
        objs = FakeObjects()
        getattr(objs, kind).add("a/one.json")
        h = make_backup(sub, objects=objs)
        with pytest.raises(BackupFailed, match="size|etag"):
            h.service.run()
        assert list(h.object_root.iterdir()) == []
        assert h.notes.events


def test_unsafe_object_keys_never_escape_the_backup_directory(tmp_path: Path) -> None:
    objs = FakeObjects(store={"artifacts": {"../../escape": b"x"}})
    h = make_backup(tmp_path, objects=objs)
    with pytest.raises(BackupFailed, match="unsafe object key"):
        h.service.run()
    assert not (tmp_path / "escape").exists()


def test_a_missing_bucket_is_a_failure(tmp_path: Path) -> None:
    h = make_backup(tmp_path, objects=FakeObjects(store={}))
    with pytest.raises(BackupFailed, match="bucket does not exist"):
        h.service.run()


def test_destination_on_the_production_device_is_refused(tmp_path: Path) -> None:
    h = make_backup(tmp_path, same_device=True)
    with pytest.raises(BackupFailed, match="same device"):
        h.service.run()
    assert h.runner.calls == []  # 何も実行する前に拒否する


def test_same_device_can_be_allowed_explicitly_and_is_recorded(tmp_path: Path) -> None:
    m = make_backup(tmp_path, same_device=True, allow_same_device=True).service.run()
    assert m["destination"]["same_device_as_source"] is True


def test_a_failing_notifier_does_not_hide_the_backup_failure(tmp_path: Path) -> None:
    h = make_backup(tmp_path, runner=FakeRunner(fail_dump={"avp"}), notes=Notes(raises=True))
    with pytest.raises(BackupFailed, match="pg_dump avp failed"):
        h.service.run()
    assert "backup.notify_failed" in events(h.lines)


def test_unchanged_objects_are_hardlinked_from_the_previous_backup(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    first = h.service.run()
    assert h.objects.downloads == ["a/one.json", "media/v.mp4"]
    h.objects.store["artifacts"]["new.json"] = b"{}"
    second = h.service.run()
    assert h.objects.downloads[2:] == ["new.json"]  # 既存は再取得しない
    assert second["objects"]["reused_from_previous"] == 2
    a = h.object_root / first["backup_id"] / "minio/artifacts/a/one.json"
    b = h.object_root / second["backup_id"] / "minio/artifacts/a/one.json"
    assert os.path.samefile(a, b)
    assert second["files"]["minio/artifacts/a/one.json"]["sha256"] == sha(b)


def test_a_changed_object_is_downloaded_again(tmp_path: Path) -> None:
    h = make_backup(tmp_path)
    h.service.run()
    h.objects.store["artifacts"]["a/one.json"] = b'{"a":2}'
    h.service.run()
    assert h.objects.downloads.count("a/one.json") == 2


def test_single_root_layout_works(tmp_path: Path) -> None:
    h = make_backup(tmp_path, single_root=True)
    m = h.service.run()
    assert m["destination"]["single_root"] is True
    assert (h.db_root / m["backup_id"] / "pg" / "avp.dump").is_file()
    assert (h.db_root / m["backup_id"] / "minio/artifacts/a/one.json").is_file()


def test_offsite_is_off_by_default_and_a_failing_offsite_keeps_the_local_backup(
    tmp_path: Path,
) -> None:
    h = make_backup(tmp_path)
    h.service.run()
    assert not any(c[0] == "offsite-cmd" for c in h.runner.calls)

    sub = tmp_path / "off"
    sub.mkdir()
    h = make_backup(sub, runner=FakeRunner(offsite_rc=3), offsite=["offsite-cmd", "{db_set}"])
    with pytest.raises(BackupFailed, match="offsite"):
        h.service.run()
    done = catalog.list_backup_ids(h.db_root)
    assert len(done) == 1  # ローカルの backup は残る
    m = catalog.read_manifest(h.db_root / done[0])
    assert m is not None and m["offsite"]["ok"] is False and m["restore_verified"] is False
    assert [e["event"] for e in h.notes.events] == ["backup.offsite_failed"]
