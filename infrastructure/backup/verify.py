"""backup 集合の at-rest 検査（restore なし）。bit-rot・欠落・余計なファイルを見つける。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from infrastructure.backup import catalog

CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_files(db_root: Path, object_root: Path, backup_id: str) -> list[str]:
    """manifest の全ファイルを再ハッシュする。問題の一覧（空なら一致）。manifest は変更しない。"""
    m = catalog.read_manifest(db_root / backup_id)
    if m is None:
        return [f"{backup_id}: manifest not readable"]
    problems: list[str] = []
    files: dict[str, Any] = m.get("files", {})
    for rel, entry in files.items():
        root = db_root if entry.get("root") == "db" else object_root
        path = root / backup_id / rel
        if not path.is_file():
            problems.append(f"{rel}: missing")
        elif _sha256(path) != entry.get("sha256"):
            problems.append(f"{rel}: sha256 mismatch")
    for root in dict.fromkeys((db_root, object_root)):
        base = root / backup_id
        for path in base.rglob("*"):
            rel = path.relative_to(base).as_posix()
            if path.is_file() and rel != "manifest.json" and rel not in files:
                problems.append(f"{rel}: not in the manifest")
    return problems
