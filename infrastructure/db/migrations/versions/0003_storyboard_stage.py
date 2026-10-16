"""storyboard 工程の語彙（ADR-0015）

0002 と同じく**CHECK 制約の貼り替えが本体**。``contracts.states`` に値を足しただけでは
実PostgreSQL の既存 CHECK が INSERT を落とす。

upgrade の語彙は ``contracts.states`` から導出する（AGENTS.md §8）。

downgrade の語彙は **ced2aae 時点の Phase 2 の値を literal で凍結する**。migration は過去の
スキーマを表す履歴なので、将来 enum が増えたり名前が変わったりしても 0003 の downgrade が
指す先（0002 の CHECK）は変わってはならない。凍結値が ced2aae の enum と一致することは
``tests/contract/test_migration_frozen_vocabulary.py`` が検査する。

downgrade は Phase 3 の値を持つ行（``storyboard_ready`` の Episode、``plan_storyboard`` の job、
``storyboard`` の Artifact、``codex_storyboard`` の予約）が残っていると CHECK の作成で**失敗する**。
行を消す・書き換えることはしない（データ移行は人間の判断）。

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

#: Phase 2（ced2aae）の CHECK 語彙。**編集しない**（履歴の凍結値）。
PHASE2_EPISODE_STATUSES: tuple[str, ...] = (
    "planned",
    "in_progress",
    "needs_work",
    "blocked",
    "ready_for_review",
    "approved",
    "uploaded",
    "analyzed",
    "completed",
    "script_ready",
    "failed",
    "cancelled",
)
PHASE2_JOB_TYPES: tuple[str, ...] = ("dummy", "write_script")
PHASE2_ARTIFACT_TYPES: tuple[str, ...] = ("dummy", "script")
PHASE2_PROVIDER_CALLS: tuple[str, ...] = ("codex_script",)

#: (table, constraint, column, enum)
_CHECKS: tuple[tuple[str, str, str, type[Enum]], ...] = (
    ("episodes", "ck_episodes_status", "status", EpisodeStatus),
    ("jobs", "ck_jobs_type", "type", JobType),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", ArtifactType),
    ("provider_reservations", "ck_provider_reservations_provider", "provider", ProviderCall),
)


def _in_check(column: str, values: tuple[str, ...]) -> str:
    allowed = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({allowed})"


def _recheck(table: str, name: str, condition: str) -> None:
    """CHECK を DROP して新しい条件で作り直す（SQLite は batch でテーブル再作成）。"""
    with op.batch_alter_table(table, schema=None) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, sa.text(condition))


def upgrade() -> None:
    for table, name, column, enum_cls in _CHECKS:
        _recheck(table, name, _in_check(column, tuple(m.value for m in enum_cls)))


#: downgrade 先（0002）の CHECK。(table, constraint, column, 凍結値)
_PHASE2_CHECKS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("episodes", "ck_episodes_status", "status", PHASE2_EPISODE_STATUSES),
    ("jobs", "ck_jobs_type", "type", PHASE2_JOB_TYPES),
    ("artifact_metadata", "ck_artifact_metadata_type", "artifact_type", PHASE2_ARTIFACT_TYPES),
    (
        "provider_reservations",
        "ck_provider_reservations_provider",
        "provider",
        PHASE2_PROVIDER_CALLS,
    ),
)


def downgrade() -> None:
    """Phase 3 の値を持つ行が残っていれば CHECK の作成で失敗する（行は触らない）。"""
    for table, name, column, values in _PHASE2_CHECKS:
        _recheck(table, name, _in_check(column, values))
