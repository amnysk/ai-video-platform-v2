"""保持の実行。削除は「検証済み」と判定され、削除直前に読み直しても検証済みの集合だけ。"""

from __future__ import annotations

from pathlib import Path

from domain.backup.retention import Policy, RetentionPlan, plan_retention
from infrastructure.backup import catalog
from infrastructure.backup.audit import AuditLog


def prune(
    db_root: Path,
    object_root: Path,
    policy: Policy,
    audit: AuditLog,
    *,
    dry_run: bool = True,
) -> RetentionPlan:
    roots = [db_root] if db_root == object_root else [db_root, object_root]
    others = tuple(r for r in roots if r != db_root)
    plan = plan_retention(catalog.catalog(db_root, others), policy)
    for warning in plan.warnings:
        audit.emit("backup.prune_warning", warning=warning)
    if dry_run:
        audit.emit("backup.prune_planned", delete=plan.delete, keep=sorted(plan.keep))
        return plan
    deleted: list[str] = []
    for backup_id in plan.delete:
        # 計画から削除までの間に manifest が変わっていないか、全 root で読み直す
        if not all(_still_verified(r, backup_id) for r in roots):
            audit.emit(
                "backup.prune_skipped", backup_id=backup_id, reason="not verified on re-read"
            )
            continue
        for root in roots:
            catalog.delete_backup_set(root, backup_id)
        deleted.append(backup_id)
    audit.emit("backup.pruned", deleted=deleted)
    plan.delete = deleted
    return plan


def _still_verified(root: Path, backup_id: str) -> bool:
    m = catalog.read_manifest(root / backup_id)
    return m is not None and m.get("restore_verified") is True and m.get("status") == "complete"
