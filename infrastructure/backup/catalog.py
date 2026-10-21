"""backup 集合（``<root>/<backup_id>/manifest.json``）の読み書きと削除。

- manifest の更新は tmp + ``os.replace``（途中で切れても壊れた manifest を残さない）
- 削除は ``<root>`` 直下の ``backup_id`` 形のディレクトリで、symlink でないものだけ
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from contracts.backup import MANIFEST_FILENAME
from domain.backup.guards import is_backup_id
from domain.backup.manifest import validate_manifest
from domain.backup.retention import CatalogEntry


def write_manifest(set_dir: Path, manifest: dict[str, Any]) -> None:
    tmp = set_dir / (MANIFEST_FILENAME + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, set_dir / MANIFEST_FILENAME)


def read_manifest(set_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((set_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_backup_ids(root: Path) -> list[str]:
    try:
        names = sorted(p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink())
    except OSError:
        return []
    return [n for n in names if is_backup_id(n)]


def catalog(root: Path, other_roots: tuple[Path, ...] = ()) -> list[CatalogEntry]:
    """``root`` の集合を CatalogEntry にする。読めない・不正・root 間で食い違うものは未検証。"""
    entries: list[CatalogEntry] = []
    for backup_id in list_backup_ids(root):
        m = read_manifest(root / backup_id)
        valid = m is not None and validate_manifest(m) == []
        verified = valid and m is not None and m.get("restore_verified") is True
        for other in other_roots:
            om = read_manifest(other / backup_id)
            if om is None or om.get("restore_verified") is not True:
                verified = False
        created = _created(m, backup_id)
        entries.append(CatalogEntry(backup_id, created, valid, bool(verified)))
    return entries


def _created(m: dict[str, Any] | None, backup_id: str) -> datetime:
    from datetime import UTC

    if m is not None:
        try:
            return datetime.fromisoformat(str(m["finished_at"]))
        except (KeyError, ValueError):
            pass
    return datetime.strptime(backup_id, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def delete_backup_set(root: Path, backup_id: str) -> bool:
    """検証済みと判定された集合の削除。形・symlink を再確認してから消す。"""
    if not is_backup_id(backup_id):
        raise ValueError(f"not a backup id: {backup_id!r}")
    target = root / backup_id
    if target.is_symlink() or not target.is_dir() or target.parent != root:
        return False
    shutil.rmtree(target)
    return True
