"""復旧先が本番でないこと・backup 先が本番と同じ故障単位でないことの判定（純粋）。

restore は**隔離した復旧先にだけ**書く。復旧先の container / project / host / port / DB / bucket /
作業ディレクトリのどれか 1 つでも本番と一致したら拒否する（全部を数えて理由を全て返す）。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass

from contracts.backup import (
    BACKUP_DATABASES,
    BACKUP_ID_PATTERN,
    PRODUCTION_BUCKETS,
    PRODUCTION_COMPOSE_PROJECT,
    PRODUCTION_DATA_PATHS,
    PRODUCTION_MINIO_CONTAINER,
    PRODUCTION_MINIO_PORTS,
    PRODUCTION_POSTGRES_CONTAINER,
    PRODUCTION_POSTGRES_PORT,
    PRODUCTION_SERVICE_HOSTS,
    PRODUCTION_TEMPORAL_PORT,
    RESTORE_CONTAINER_PREFIX,
)

_PRODUCTION_PORTS = frozenset(
    {PRODUCTION_POSTGRES_PORT, PRODUCTION_TEMPORAL_PORT, *PRODUCTION_MINIO_PORTS}
)
_PRODUCTION_DBS = frozenset({*BACKUP_DATABASES, "postgres", "avp_test", "template0", "template1"})


class ProductionTargetRefused(Exception):
    """復旧先が本番（または本番と区別できない）。理由を全て持つ。"""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass(frozen=True)
class RestoreTarget:
    container_name: str
    compose_project: str
    pg_host: str
    pg_port: int
    databases: tuple[str, ...]
    buckets: tuple[str, ...]
    work_dir: str


def _under(path: str, parent: str) -> bool:
    p, q = posixpath.normpath(path), posixpath.normpath(parent)
    return p == q or p.startswith(q.rstrip("/") + "/")


def target_problems(target: RestoreTarget, protected_paths: Iterable[str] = ()) -> list[str]:
    """復旧先が本番と衝突する理由の一覧。空なら隔離されている。"""
    problems: list[str] = []
    if target.compose_project == PRODUCTION_COMPOSE_PROJECT:
        problems.append(f"compose project is production ({target.compose_project})")
    if target.container_name in {PRODUCTION_POSTGRES_CONTAINER, PRODUCTION_MINIO_CONTAINER}:
        problems.append(f"container is production ({target.container_name})")
    if not target.container_name.startswith(RESTORE_CONTAINER_PREFIX):
        problems.append(f"container name must start with {RESTORE_CONTAINER_PREFIX!r}")
    if target.pg_host.strip().lower() in PRODUCTION_SERVICE_HOSTS:
        problems.append(f"host is production ({target.pg_host})")
    if target.pg_port in _PRODUCTION_PORTS:
        problems.append(f"port is production ({target.pg_port})")
    for db in target.databases:
        if db in _PRODUCTION_DBS:
            problems.append(f"database is production ({db})")
    if not target.databases:
        problems.append("no target database")
    for bucket in target.buckets:
        if bucket in PRODUCTION_BUCKETS:
            problems.append(f"bucket is production ({bucket})")
    if not target.buckets:
        problems.append("no target bucket")
    if not target.work_dir or not posixpath.isabs(target.work_dir):
        problems.append("work_dir must be an absolute path")
    for protected in (*PRODUCTION_DATA_PATHS, *protected_paths):
        if target.work_dir and (
            _under(target.work_dir, protected) or _under(protected, target.work_dir)
        ):
            problems.append(f"work_dir overlaps a protected path ({protected})")
    return problems


def assert_isolated(target: RestoreTarget, protected_paths: Iterable[str] = ()) -> None:
    problems = target_problems(target, protected_paths)
    if problems:
        raise ProductionTargetRefused(problems)


def destination_problems(
    role: str, destination_device: int, source_device: int, *, allow_same_device: bool
) -> list[str]:
    """backup 先が backup 対象と同じディスクなら理由を返す（同じ故障で両方失う）。"""
    if destination_device == source_device and not allow_same_device:
        return [
            f"{role}: destination is on the same device as the production data it protects"
            " (a disk failure would lose both)"
        ]
    return []


def is_backup_id(name: str) -> bool:
    return re.fullmatch(BACKUP_ID_PATTERN, name) is not None


def safe_relative_key(prefix: str, key: str) -> str | None:
    """オブジェクト key を backup 内の相対パスにする。絶対・``..``・空要素・NUL は None（拒否）。"""
    if not key or "\x00" in key or key.startswith("/") or "\\" in key:
        return None
    parts = key.split("/")
    if any(p in {"", ".", ".."} for p in parts):
        return None
    return posixpath.join(prefix, *parts)
