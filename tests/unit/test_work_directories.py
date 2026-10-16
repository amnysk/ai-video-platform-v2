"""一時作業領域の安全条件（ADR-0016、docs/operations/work-directories.md）。"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from domain.errors import WorkspaceUnavailableError
from infrastructure.config import Settings
from infrastructure.workdir import MINIO_DATA_DIR, WorkDirectory

EP = str(uuid.UUID(int=1))
JOB = str(uuid.UUID(int=2))
OTHER_JOB = str(uuid.UUID(int=3))


def _wd(root: Path) -> WorkDirectory:
    return WorkDirectory(root, forbidden=())


def test_settings_defaults() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.ai_video_work_root == "/mnt/minio-hdd/ai-video-work"
    assert settings.openmontage_repo_path is None
    assert settings.openmontage_commit == "2fa571e39ad0632148dad77c7a2134f7e6fe0797"
    assert settings.storyboard_timeout_seconds == 900


def test_default_forbidden_dir_is_minio_data() -> None:
    with pytest.raises(WorkspaceUnavailableError):
        WorkDirectory(MINIO_DATA_DIR / "work")


def test_create_builds_the_layout(tmp_path: Path) -> None:
    work = _wd(tmp_path / "root").create(EP, JOB)
    assert work.base == tmp_path / "root" / "episodes" / EP / JOB
    for sub in (work.input, work.output, work.openmontage, work.tmp):
        assert sub.is_dir() and sub.parent == work.base
    assert not (work.base / "render").exists()


def test_create_is_idempotent(tmp_path: Path) -> None:
    wd = _wd(tmp_path)
    assert wd.create(EP, JOB) == wd.create(EP, JOB)


def test_uuid_ids_are_canonicalized(tmp_path: Path) -> None:
    work = _wd(tmp_path).create(EP.upper(), JOB.replace("-", ""))
    assert work.base == tmp_path / "episodes" / EP / JOB


@pytest.mark.parametrize("value", ["../outside", "nested/path", "", ".", "..", "episode-1"])
def test_non_uuid_ids_are_rejected(tmp_path: Path, value: str) -> None:
    wd = _wd(tmp_path)
    with pytest.raises(WorkspaceUnavailableError):
        wd.create(value, JOB)
    with pytest.raises(WorkspaceUnavailableError):
        wd.create(EP, value)
    with pytest.raises(WorkspaceUnavailableError):
        wd.cleanup(EP, value)


def test_relative_root_is_rejected() -> None:
    with pytest.raises(WorkspaceUnavailableError):
        WorkDirectory("relative/work", forbidden=())


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    wd = _wd(link)
    with pytest.raises(WorkspaceUnavailableError):
        wd.create(EP, JOB)
    assert not (real / "episodes").exists()


@pytest.mark.parametrize("relation", ["same", "inside", "contains"])
def test_root_overlapping_forbidden_dir_is_rejected(tmp_path: Path, relation: str) -> None:
    forbidden = tmp_path / "minio-hdd" / "data"
    root = {
        "same": forbidden,
        "inside": forbidden / "work",
        "contains": tmp_path / "minio-hdd",
    }[relation]
    with pytest.raises(WorkspaceUnavailableError):
        WorkDirectory(root, forbidden=(forbidden,))


def test_root_resolving_into_forbidden_dir_is_rejected(tmp_path: Path) -> None:
    forbidden = tmp_path / "data"
    forbidden.mkdir()
    link = tmp_path / "work"
    link.symlink_to(forbidden, target_is_directory=True)
    wd = WorkDirectory(link, forbidden=(forbidden,))
    with pytest.raises(WorkspaceUnavailableError):
        wd.create(EP, JOB)


def test_cleanup_removes_only_the_job_and_keeps_siblings(tmp_path: Path) -> None:
    wd = _wd(tmp_path)
    work = wd.create(EP, JOB)
    sibling = wd.create(EP, OTHER_JOB)
    (work.tmp / "x.txt").write_text("x", encoding="utf-8")
    (sibling.output / "keep.txt").write_text("keep", encoding="utf-8")

    assert wd.cleanup(EP, JOB) is True
    assert not work.base.exists()
    assert (sibling.output / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert (tmp_path / "episodes" / EP).is_dir()


def test_cleanup_of_missing_dir_returns_false(tmp_path: Path) -> None:
    assert _wd(tmp_path / "never-created").cleanup(EP, JOB) is False
    assert _wd(tmp_path).cleanup(EP, JOB) is False


def test_cleanup_refuses_job_symlink_to_sibling_inside_root(tmp_path: Path) -> None:
    """参照実装のバグの回帰: resolve() 後の root 配下検査は同じ root 内の別 job を消していた。"""
    wd = _wd(tmp_path)
    sibling = wd.create(EP, OTHER_JOB)
    (sibling.output / "keep.txt").write_text("keep", encoding="utf-8")
    (tmp_path / "episodes" / EP / JOB).symlink_to(sibling.base, target_is_directory=True)

    with pytest.raises(WorkspaceUnavailableError):
        wd.cleanup(EP, JOB)
    assert (sibling.output / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_cleanup_refuses_episode_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    (outside / JOB).mkdir(parents=True)
    root = tmp_path / "root"
    (root / "episodes").mkdir(parents=True)
    (root / "episodes" / EP).symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceUnavailableError):
        _wd(root).cleanup(EP, JOB)
    assert (outside / JOB).is_dir()


def test_create_refuses_escaping_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "episodes").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WorkspaceUnavailableError):
        _wd(root).create(EP, JOB)
    assert list(outside.iterdir()) == []


def test_attempt_directories_are_isolated(tmp_path) -> None:
    import uuid as _uuid

    import pytest as _pytest

    from domain.errors import WorkspaceUnavailableError
    from infrastructure.workdir import WorkDirectory

    wd = WorkDirectory(tmp_path / "root", forbidden=())
    ep, job = str(_uuid.uuid4()), str(_uuid.uuid4())
    one = wd.create(ep, job, attempt=1)
    two = wd.create(ep, job, attempt=2)
    (one.output / "x").write_bytes(b"1")
    (two.output / "x").write_bytes(b"2")
    assert one.base.name == "attempt-1" and one.base.parent == two.base.parent

    assert wd.cleanup(ep, job, attempt=1) is True
    assert not one.base.exists() and (two.output / "x").read_bytes() == b"2"
    for bad in (0, -1, True):
        with _pytest.raises(WorkspaceUnavailableError):
            wd.create(ep, job, attempt=bad)


def test_attempt_cleanup_refuses_symlinked_attempt(tmp_path) -> None:
    import os
    import uuid as _uuid

    import pytest as _pytest

    from domain.errors import WorkspaceUnavailableError
    from infrastructure.workdir import WorkDirectory

    wd = WorkDirectory(tmp_path / "root", forbidden=())
    ep, job = str(_uuid.uuid4()), str(_uuid.uuid4())
    base = wd.create(ep, job).base
    victim = tmp_path / "victim"
    victim.mkdir()
    os.symlink(victim, base / "attempt-1")
    with _pytest.raises(WorkspaceUnavailableError):
        wd.cleanup(ep, job, attempt=1)
    assert victim.exists()
