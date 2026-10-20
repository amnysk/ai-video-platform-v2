"""backup manifest の組み立て・検証・復旧演習の記録（純粋）。

``restore_verified`` を True にできるのは ``apply_drill`` だけ（証跡が全て pass のときだけ）。
``new_manifest`` は常に False で作る。release preflight が読む
``components.<name>.created_at`` を保つ
（``domain/release_preflight.BACKUP_COMPONENTS`` と同じ名前。一致は contract test が検査する）。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from contracts.backup import (
    BACKUP_DATABASES,
    COMPONENT_DB_DUMP,
    COMPONENT_MINIO,
    COMPONENT_TEMPORAL,
    MANIFEST_SCHEMA_VERSION,
    STATUS_COMPLETE,
    TEMPORAL_DATABASES,
)

REQUIRED_DRILL_CHECKS = (
    "files_at_rest_checksums",
    "databases_restored",
    "row_counts",
    "alembic_revision",
    "object_checksums",
    "reference_integrity",
)


class DrillRejected(Exception):
    """演習の証跡が不十分（restore_verified を True にしない）。"""


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.isoformat()


def new_manifest(
    *,
    backup_id: str,
    started_at: datetime,
    finished_at: datetime,
    git_revision: str,
    git_dirty: bool,
    alembic_revision: str,
    tool_versions: Mapping[str, str],
    databases: Mapping[str, Mapping[str, Any]],
    objects: Mapping[str, Any],
    files: Mapping[str, Mapping[str, Any]],
    consistency: Mapping[str, Any],
    destination: Mapping[str, Any],
    offsite: Mapping[str, Any],
) -> dict[str, Any]:
    """``restore_verified`` は常に False。component の created_at は各部分の完了時刻。"""
    db_done = [databases[d]["finished_at"] for d in databases if d not in TEMPORAL_DATABASES]
    tmp_done = [databases[d]["finished_at"] for d in databases if d in TEMPORAL_DATABASES]
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "backup_id": backup_id,
        "status": STATUS_COMPLETE,
        "restore_verified": False,
        "restore_drill": None,
        "started_at": iso(started_at),
        "finished_at": iso(finished_at),
        "git": {"revision": git_revision, "dirty": git_dirty},
        "alembic_revision": alembic_revision,
        "tool_versions": dict(tool_versions),
        "consistency": dict(consistency),
        "components": {
            COMPONENT_DB_DUMP: {"created_at": max(db_done), "databases": _names(databases, False)},
            COMPONENT_TEMPORAL: {"created_at": max(tmp_done), "databases": _names(databases, True)},
            COMPONENT_MINIO: {
                "created_at": objects["finished_at"],
                "buckets": sorted(objects["buckets"]),
            },
        },
        "databases": {k: dict(v) for k, v in databases.items()},
        "objects": copy.deepcopy(dict(objects)),
        "files": {k: dict(v) for k, v in sorted(files.items())},
        "destination": dict(destination),
        "offsite": dict(offsite),
    }


def _names(databases: Mapping[str, Any], temporal: bool) -> list[str]:
    return sorted(d for d in databases if (d in TEMPORAL_DATABASES) == temporal)


def validate_manifest(m: object) -> list[str]:
    """形の検査。問題の一覧（空なら有効）。"""
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    problems: list[str] = []
    if m.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        problems.append(f"schema_version != {MANIFEST_SCHEMA_VERSION}")
    for key in ("backup_id", "started_at", "finished_at", "alembic_revision"):
        if not isinstance(m.get(key), str) or not m.get(key):
            problems.append(f"missing {key}")
    if m.get("status") != STATUS_COMPLETE:
        problems.append("status is not complete")
    if not isinstance(m.get("restore_verified"), bool):
        problems.append("restore_verified must be a bool")
    if m.get("restore_verified") is True and not isinstance(m.get("restore_drill"), dict):
        problems.append("restore_verified without restore_drill evidence")
    comps = m.get("components")
    if not isinstance(comps, dict):
        problems.append("components missing")
    else:
        for name in (COMPONENT_DB_DUMP, COMPONENT_MINIO, COMPONENT_TEMPORAL):
            entry = comps.get(name)
            if not isinstance(entry, dict) or not isinstance(entry.get("created_at"), str):
                problems.append(f"components.{name}.created_at missing")
    dbs = m.get("databases")
    if not isinstance(dbs, dict) or set(dbs) != set(BACKUP_DATABASES):
        problems.append(f"databases must be exactly {sorted(BACKUP_DATABASES)}")
    else:
        for name, entry in dbs.items():
            ok = isinstance(entry, dict) and isinstance(entry.get("file"), str)
            if (
                not ok
                or not _is_sha(entry.get("sha256"))
                or not isinstance(entry.get("rows"), dict)
            ):
                problems.append(f"databases.{name} incomplete")
    files = m.get("files")
    if not isinstance(files, dict) or not files:
        problems.append("files missing")
    else:
        bad = [
            k for k, v in files.items() if not isinstance(v, dict) or not _is_sha(v.get("sha256"))
        ]
        if bad:
            problems.append(f"files without sha256: {len(bad)}")
    return problems


def _is_sha(v: object) -> bool:
    return isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def row_count_problems(
    recorded: Mapping[str, Mapping[str, Mapping[str, int]]],
    restored: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """復旧後の行数が backup 時の before..after に収まるか。表の増減は不一致。"""
    problems: list[str] = []
    for db, tables in recorded.items():
        got = restored.get(db)
        if got is None:
            problems.append(f"{db}: not restored")
            continue
        for table in sorted(set(tables) | set(got)):
            span = tables.get(table)
            if span is None:
                problems.append(f"{db}.{table}: unexpected table")
            elif table not in got:
                problems.append(f"{db}.{table}: missing after restore")
            else:
                lo, hi = sorted((span["before"], span["after"]))
                if not lo <= got[table] <= hi:
                    problems.append(f"{db}.{table}: restored {got[table]} not in [{lo}, {hi}]")
    return problems


def reference_problems(
    rows: list[tuple[str, str, str]],
    objects: Mapping[tuple[str, str], str],
    backed_up_buckets: set[str],
) -> list[str]:
    """DB の (bucket, object_key, sha256) が全て、復旧した本体と一致するか（PG↔MinIO 整合）。"""
    problems: list[str] = []
    for bucket, key, sha in rows:
        if bucket not in backed_up_buckets:
            problems.append(f"{bucket}/{key}: bucket not in the backup")
            continue
        actual = objects.get((bucket, key))
        if actual is None:
            problems.append(f"{bucket}/{key}: object missing")
        elif actual != sha:
            problems.append(f"{bucket}/{key}: sha256 differs from the DB row")
    return problems


@dataclass(frozen=True)
class DrillEvidence:
    backup_id: str
    at: datetime
    target: Mapping[str, Any]
    checks: Mapping[str, Mapping[str, Any]]  # name -> {"passed": bool, "detail": ...}
    tool_versions: Mapping[str, str]


def evidence_passed(e: DrillEvidence) -> list[str]:
    """失敗・欠けている検査の一覧（空なら全 pass）。"""
    problems = [f"check missing: {c}" for c in REQUIRED_DRILL_CHECKS if c not in e.checks]
    problems += [f"check failed: {n}" for n, v in e.checks.items() if v.get("passed") is not True]
    return problems


def apply_drill(manifest: Mapping[str, Any], evidence: DrillEvidence) -> dict[str, Any]:
    """演習の結果を manifest へ書く。pass のときだけ ``restore_verified`` を True にする。

    変えるのは ``restore_verified`` と ``restore_drill`` だけ（他は backup 時のまま）。
    """
    if evidence.backup_id != manifest.get("backup_id"):
        raise DrillRejected("evidence is for a different backup")
    problems = evidence_passed(evidence)
    drill = {
        "at": iso(evidence.at),
        "result": "fail" if problems else "pass",
        "problems": problems,
        "target": dict(evidence.target),
        "checks": {k: dict(v) for k, v in evidence.checks.items()},
        "tool_versions": dict(evidence.tool_versions),
    }
    drill["evidence_sha256"] = hashlib.sha256(
        json.dumps(drill, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()
    out = copy.deepcopy(dict(manifest))
    out["restore_drill"] = drill
    out["restore_verified"] = not problems
    return out
