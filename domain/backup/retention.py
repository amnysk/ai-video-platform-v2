"""保持（daily / weekly / monthly）の計画。**検証済みでないものは決して消さない**（純粋）。

削除候補になるのは次を全て満たすものだけ:
  complete かつ restore_verified、方針（日・週・月ごとの最新）で選ばれていない、
  かつ「最新の検証済み」ではない。
未検証・未完了・読めないものは常に残す（警告だけ出す）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from contracts.backup import DEFAULT_KEEP_DAILY, DEFAULT_KEEP_MONTHLY, DEFAULT_KEEP_WEEKLY


@dataclass(frozen=True)
class CatalogEntry:
    backup_id: str
    created_at: datetime
    complete: bool
    restore_verified: bool


@dataclass(frozen=True)
class Policy:
    keep_daily: int = DEFAULT_KEEP_DAILY
    keep_weekly: int = DEFAULT_KEEP_WEEKLY
    keep_monthly: int = DEFAULT_KEEP_MONTHLY


@dataclass
class RetentionPlan:
    keep: dict[str, list[str]] = field(default_factory=dict)
    delete: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _newest_per_bucket(entries: list[CatalogEntry], key: object, limit: int) -> list[CatalogEntry]:
    seen: dict[object, CatalogEntry] = {}
    for e in sorted(entries, key=lambda x: x.created_at, reverse=True):
        k = key(e)  # type: ignore[operator]
        if k not in seen:
            seen[k] = e
        if len(seen) >= limit:
            break
    return list(seen.values())


def plan_retention(entries: list[CatalogEntry], policy: Policy | None = None) -> RetentionPlan:
    policy = policy or Policy()
    plan = RetentionPlan()

    def keep(e: CatalogEntry, why: str) -> None:
        plan.keep.setdefault(e.backup_id, []).append(why)

    complete = [e for e in entries if e.complete]
    for e in entries:
        if not e.complete:
            keep(e, "incomplete")
        elif not e.restore_verified:
            keep(e, "unverified")
    if policy.keep_daily > 0:
        for e in _newest_per_bucket(complete, lambda x: x.created_at.date(), policy.keep_daily):
            keep(e, "daily")
    if policy.keep_weekly > 0:
        weekly = lambda x: x.created_at.isocalendar()[:2]  # noqa: E731
        for e in _newest_per_bucket(complete, weekly, policy.keep_weekly):
            keep(e, "weekly")
    if policy.keep_monthly > 0:
        monthly = lambda x: (x.created_at.year, x.created_at.month)  # noqa: E731
        for e in _newest_per_bucket(complete, monthly, policy.keep_monthly):
            keep(e, "monthly")
    verified = sorted(
        (e for e in complete if e.restore_verified), key=lambda x: x.created_at, reverse=True
    )
    if verified:
        keep(verified[0], "last-verified")
    else:
        plan.warnings.append("no restore-verified backup exists: run the restore drill")
    unverified = [e.backup_id for e in entries if not (e.complete and e.restore_verified)]
    if unverified:
        plan.warnings.append(f"{len(unverified)} backup(s) not restore-verified are kept")
    plan.delete = sorted(e.backup_id for e in entries if e.backup_id not in plan.keep)
    return plan
