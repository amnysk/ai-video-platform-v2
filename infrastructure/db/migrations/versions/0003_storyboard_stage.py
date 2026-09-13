"""storyboard 工程の語彙（ADR-0015）

0002 と同じく**CHECK 制約の貼り替えが本体**。``contracts.states`` に値を足しただけでは
実PostgreSQL の既存 CHECK が INSERT を落とす。

語彙は ``contracts.states`` から導出する（AGENTS.md §8）。downgrade だけが
Phase 2 の語彙を持つが、それも「Phase 3 で足した値を除く」形で導出する。

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-13
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import ArtifactType, EpisodeStatus, JobType, ProviderCall

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None

#: Phase 3 で足した値。downgrade で Phase 2 の語彙を復元するためだけに持つ。
_PHASE3_ADDITIONS: set[Enum] = {
    EpisodeStatus.STORYBOARD_READY,
    JobType.PLAN_STORYBOARD,
    ArtifactType.STORYBOARD,
    ProviderCall.CODEX_STORYBOARD,
}

#: (table, constraint, column, enum)
_CHECKS: tuple[tuple[str, str, str, type[Enum]], ...] = (
    ("episodes", "ck_episodes_status", "status", EpisodeStatus),
    ("jobs", "ck_jobs_type", "type", JobType),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", ArtifactType),
    ("provider_reservations", "ck_provider_reservations_provider", "provider", ProviderCall),
)


def _in_check(column: str, members: list[Enum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in members)
    return f"{column} IN ({allowed})"


def _recheck(table: str, name: str, condition: str) -> None:
    """CHECK を DROP して新しい条件で作り直す（SQLite は batch でテーブル再作成）。"""
    with op.batch_alter_table(table, schema=None) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, sa.text(condition))


def upgrade() -> None:
    for table, name, column, enum_cls in _CHECKS:
        _recheck(table, name, _in_check(column, list(enum_cls)))


def downgrade() -> None:
    for table, name, column, enum_cls in _CHECKS:
        members = [m for m in enum_cls if m not in _PHASE3_ADDITIONS]
        _recheck(table, name, _in_check(column, members))
