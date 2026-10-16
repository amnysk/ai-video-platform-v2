"""AI 動画生成の一時作業領域（ADR-0016、docs/operations/work-directories.md）。

正式な成果物は ArtifactStore 経由で MinIO に置く。ここが扱うのは再生成可能な中間ファイルだけで、
**source of truth ではない**。

安全条件:

- root は絶対パスで、途中に symlink を含まない（``resolve() == normpath``）
- root は MinIO のデータディレクトリと重ならない
- episode / job ID は UUID に正規化する（パス区切り・``..`` を構造的に排除）
- 作成・削除とも各構成要素を ``lstat`` で検査し、symlink を拒否する
- 削除は ``resolve()`` しない（root 内の別 job への symlink を辿って他人を消さない）

違反と OS エラーはすべて ``WorkspaceUnavailableError`` に写像する。
"""

from __future__ import annotations

import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from domain.errors import WorkspaceUnavailableError

#: MinIO だけが管理するデータディレクトリ。アプリはここを読み書きしない。
MINIO_DATA_DIR = Path("/mnt/minio-hdd/minio-data")

_SUBDIRS = ("input", "output", "openmontage", "tmp")


@dataclass(frozen=True, slots=True)
class JobWorkDir:
    """1 job の作業領域。"""

    base: Path
    input: Path
    output: Path
    openmontage: Path
    tmp: Path


def _canonical_uuid(value: str, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise WorkspaceUnavailableError(f"{field} must be a UUID: {value!r}") from exc


def _overlaps(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _require_real_dir(path: Path) -> None:
    """``path`` が symlink でない実ディレクトリであること。"""
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        raise WorkspaceUnavailableError(f"refusing symlink in work directory: {path}")
    if not stat.S_ISDIR(mode):
        raise WorkspaceUnavailableError(f"not a directory: {path}")


class WorkDirectory:
    """episode/job 単位の一時作業領域を作り、消す。"""

    def __init__(self, root: str | Path, *, forbidden: Iterable[str | Path] = (MINIO_DATA_DIR,)):
        raw = Path(root)
        if not raw.is_absolute():
            raise WorkspaceUnavailableError(f"work root must be absolute: {root!s}")
        self._root = Path(os.path.normpath(raw))
        self._forbidden = tuple(Path(os.path.normpath(Path(p))) for p in forbidden)
        for denied in self._forbidden:
            if _overlaps(self._root, denied):
                raise WorkspaceUnavailableError(
                    f"work root {self._root} overlaps a forbidden directory {denied}"
                )

    @property
    def root(self) -> Path:
        return self._root

    def create(self, episode_id: str, job_id: str, *, attempt: int | None = None) -> JobWorkDir:
        """作業領域を作って返す。既にあれば再利用する（冪等）。

        ``attempt`` を渡すと ``<job>/attempt-<n>/`` を作る。同じ job の試行が重なっても（前の試行が
        heartbeat timeout 後もまだ走っている等）互いのファイルを消さない。
        """
        base = self._job_dir(episode_id, job_id, attempt)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            self._check_root()
            current = self._root
            for part in base.relative_to(self._root).parts:
                current = current / part
                self._mkdir_real(current)
            paths = {name: base / name for name in _SUBDIRS}
            for path in paths.values():
                self._mkdir_real(path)
        except OSError as exc:
            raise WorkspaceUnavailableError(f"cannot create work directory {base}: {exc}") from exc
        return JobWorkDir(base=base, **paths)

    def cleanup(self, episode_id: str, job_id: str, *, attempt: int | None = None) -> bool:
        """job（``attempt`` を渡せばその試行だけ）の作業領域を削除する。

        無ければ ``False``。symlink を含めば拒否する。
        """
        base = self._job_dir(episode_id, job_id, attempt)
        try:
            if not os.path.lexists(self._root):
                return False
            self._check_root()
            current = self._root
            for part in base.relative_to(self._root).parts:
                current = current / part
                if not os.path.lexists(current):
                    return False
                _require_real_dir(current)
            shutil.rmtree(base)
        except OSError as exc:
            raise WorkspaceUnavailableError(
                f"cannot clean up work directory {base}: {exc}"
            ) from exc
        return True

    def _job_dir(self, episode_id: str, job_id: str, attempt: int | None = None) -> Path:
        episode = _canonical_uuid(episode_id, "episode_id")
        job = _canonical_uuid(job_id, "job_id")
        base = self._root / "episodes" / episode / job
        if attempt is None:
            return base
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
            raise WorkspaceUnavailableError(f"attempt must be a positive int: {attempt!r}")
        return base / f"attempt-{attempt}"

    def _check_root(self) -> None:
        resolved = self._root.resolve(strict=True)
        if resolved != self._root:
            raise WorkspaceUnavailableError(
                f"work root must not contain symlinks: {self._root} -> {resolved}"
            )
        _require_real_dir(self._root)
        for denied in self._forbidden:
            if _overlaps(resolved, denied):
                raise WorkspaceUnavailableError(
                    f"work root {resolved} overlaps a forbidden directory {denied}"
                )

    @staticmethod
    def _mkdir_real(path: Path) -> None:
        if not os.path.lexists(path):
            path.mkdir()
        _require_real_dir(path)


__all__ = ["MINIO_DATA_DIR", "JobWorkDir", "WorkDirectory"]
