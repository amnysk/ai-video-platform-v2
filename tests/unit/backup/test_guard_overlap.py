"""backup root が保護パス（MinIO データ dir 等）と重ならないこと（realpath・flag 無関係）。

理由: docs/testing/backup-restore-rationale.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from domain.backup.guards import (
    RestoreTarget,
    root_overlap_problems,
    target_problems,
)
from infrastructure.backup.service import BackupFailed
from tests.unit.backup.fakes import make_backup


def test_a_root_inside_or_around_a_protected_path_is_refused(tmp_path: Path) -> None:
    protected = tmp_path / "minio-data"
    protected.mkdir()
    assert root_overlap_problems("db", protected / "backups", [str(protected)])
    assert root_overlap_problems("db", protected, [str(protected)])
    assert root_overlap_problems("db", tmp_path, [str(protected)])  # 祖先が保護パスを含む
    assert root_overlap_problems("db", tmp_path / "other", [str(protected)]) == []


def test_a_symlink_into_a_protected_path_is_refused(tmp_path: Path) -> None:
    protected = tmp_path / "minio-data"
    protected.mkdir()
    link = tmp_path / "innocent"
    link.symlink_to(protected)
    assert root_overlap_problems("db", link / "b", [str(protected)])
    # 保護パス側が symlink でも同じ
    alias = tmp_path / "alias"
    alias.symlink_to(protected)
    assert root_overlap_problems("db", protected / "b", [str(alias)])


def test_dotdot_cannot_hide_the_overlap(tmp_path: Path) -> None:
    protected = tmp_path / "minio-data"
    protected.mkdir()
    sneaky = tmp_path / "x" / ".." / "minio-data" / "b"
    assert root_overlap_problems("objects", sneaky, [str(protected)])


def test_restore_work_dir_symlink_into_a_protected_path_is_refused(tmp_path: Path) -> None:
    protected = tmp_path / "minio-data"
    protected.mkdir()
    link = tmp_path / "work"
    link.symlink_to(protected)
    target = RestoreTarget(
        container_name="avp-restore-x",
        compose_project="avp-restore-x",
        pg_host="docker-exec",
        pg_port=0,
        databases=("restore_avp",),
        buckets=("restore-artifacts",),
        work_dir=str(link / "avp-restore-x"),
    )
    assert any("protected" in p for p in target_problems(target, [str(protected)]))


@pytest.mark.parametrize("allow", [False, True])
def test_the_service_refuses_a_root_inside_the_protected_path_even_with_allow_same_device(
    tmp_path: Path, allow: bool
) -> None:
    h = make_backup(tmp_path, same_device=True, allow_same_device=allow)
    h.service._cfg.protected_paths = (str(tmp_path),)  # noqa: SLF001
    with pytest.raises(BackupFailed, match="protected"):
        h.service.run()
    assert h.objects.downloads == []
