"""episodes / jobs / artifact_metadata の初期スキーマ

状態値の語彙は contracts.states から導出する。ここに literal で書き直さない
（AGENTS.md §8）。

Revision ID: 0001
Revises:
Create Date: 2026-09-04
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import ArtifactType, EpisodeStatus, JobStatus, JobType

revision: str = "0001"
down_revision: str | None = None
branch_labels = None
depends_on = None


def _in_check(column: str, enum_cls: type[Enum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({allowed})"


def upgrade() -> None:
    op.create_table(
        "episodes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("topic", sa.String(length=500), nullable=True),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("blocked_reason", sa.String(length=1000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(_in_check("status", EpisodeStatus), name="ck_episodes_status"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("error_summary", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_in_check("status", JobStatus), name="ck_jobs_status"),
        sa.CheckConstraint(_in_check("type", JobType), name="ck_jobs_type"),
        sa.CheckConstraint("attempts >= 0", name="ck_jobs_attempts_non_negative"),
        sa.CheckConstraint("max_attempts >= 1", name="ck_jobs_max_attempts_positive"),
    )
    op.create_index("ix_jobs_episode_id", "jobs", ["episode_id"])

    op.create_table(
        "artifact_metadata",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=32), nullable=False),
        sa.Column("schema_version", sa.String(length=16), nullable=False),
        sa.Column("bucket", sa.String(length=128), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("produced_by_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["produced_by_job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            _in_check("artifact_type", ArtifactType), name="ck_artifact_metadata_type"
        ),
        sa.UniqueConstraint(
            "episode_id", "artifact_type", "sha256", name="uq_artifact_metadata_content"
        ),
    )
    op.create_index("ix_artifact_metadata_episode_id", "artifact_metadata", ["episode_id"])


def downgrade() -> None:
    op.drop_index("ix_artifact_metadata_episode_id", table_name="artifact_metadata")
    op.drop_table("artifact_metadata")
    op.drop_index("ix_jobs_episode_id", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("episodes")
