"""台本工程のための Artifact 世代管理と予約台帳（ADR-0012 / ADR-0013）

CHECK 制約の**貼り替えが本体**である。実PostgreSQL には 0001 が焼いた
``CHECK (type = 'dummy')`` / ``CHECK (artifact_type = 'dummy')`` /
``CHECK (status IN (...))`` が残っており、``contracts.states`` に値を足すだけでは
INSERT が落ちる。SQLite のテストDBは毎回作り直されるのでこの事故を検出できない。
そのため語彙が増えた CHECK は DROP して新語彙で再作成する。

語彙は ``contracts.states`` から導出する。ここに literal で書き直さない
（AGENTS.md §8）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04
"""

from __future__ import annotations

from enum import Enum

import sqlalchemy as sa
from alembic import op

from contracts.states import (
    ArtifactType,
    EpisodeStatus,
    FailureClass,
    JobStatus,
    JobType,
    ProviderCall,
    ReservationStatus,
)

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels = None
depends_on = None


def _in_check(column: str, enum_cls: type[Enum]) -> str:
    allowed = ", ".join(f"'{member.value}'" for member in enum_cls)
    return f"{column} IN ({allowed})"


#: 0001 が焼いた「Phase 1 の語彙」。downgrade で復元するためだけに持つ。
_PHASE1_VOCABULARY = {
    "episodes": {
        "ck_episodes_status": "status IN ("
        + ", ".join(f"'{m.value}'" for m in EpisodeStatus if m is not EpisodeStatus.SCRIPT_READY)
        + ")"
    },
    "jobs": {"ck_jobs_type": f"type IN ('{JobType.DUMMY.value}')"},
    "artifact_metadata": {
        "ck_artifact_metadata_type": f"artifact_type IN ('{ArtifactType.DUMMY.value}')"
    },
}


def _recheck(table: str, name: str, condition: str) -> None:
    """CHECK を DROP して新しい条件で作り直す。

    SQLite は ALTER で CHECK を落とせないので batch_alter_table
    （テーブル再作成）に載せる。``render_as_batch=True`` は env.py で有効。
    """
    with op.batch_alter_table(table, schema=None) as batch:
        batch.drop_constraint(name, type_="check")
        batch.create_check_constraint(name, sa.text(condition))


def upgrade() -> None:
    # --- 1. 語彙が増えた CHECK の貼り替え -------------------------------
    _recheck("episodes", "ck_episodes_status", _in_check("status", EpisodeStatus))
    _recheck("jobs", "ck_jobs_type", _in_check("type", JobType))
    _recheck(
        "artifact_metadata",
        "ck_artifact_metadata_type",
        _in_check("artifact_type", ArtifactType),
    )

    # --- 2. artifact_metadata に世代管理の3列（ADR-0012） ----------------
    # 既存行（Phase 1 の dummy）の input_hash は再計算できないので、
    # 決定論的な生成器の content sha256 を流用する。
    with op.batch_alter_table("artifact_metadata", schema=None) as batch:
        batch.add_column(sa.Column("input_hash", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("version", sa.Integer(), nullable=False, server_default="1"))
        batch.add_column(sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True))

    op.execute(sa.text("UPDATE artifact_metadata SET input_hash = sha256"))

    with op.batch_alter_table("artifact_metadata", schema=None) as batch:
        batch.alter_column("input_hash", existing_type=sa.String(length=64), nullable=False)
        batch.create_unique_constraint(
            "uq_artifact_metadata_version", ["episode_id", "artifact_type", "version"]
        )

    # 現行世代は常に1本（partial unique index）。SQLite / PostgreSQL の両方が
    # partial index をサポートするので、同じ WHERE 句をそのまま渡す。
    op.create_index(
        "uq_artifact_metadata_current",
        "artifact_metadata",
        ["episode_id", "artifact_type"],
        unique=True,
        sqlite_where=sa.text("superseded_at IS NULL"),
        postgresql_where=sa.text("superseded_at IS NULL"),
    )

    # --- 3. 予約台帳（ADR-0013） ---------------------------------------
    op.create_table(
        "provider_reservations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("episode_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("input_hash", sa.String(length=64), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("raw_output_key", sa.String(length=1024), nullable=True),
        sa.Column("outcome_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("error_summary", sa.String(length=2000), nullable=True),
        sa.Column(
            "reserved_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reconciled_by", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["episode_id"], ["episodes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["outcome_artifact_id"], ["artifact_metadata.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            _in_check("provider", ProviderCall), name="ck_provider_reservations_provider"
        ),
        sa.CheckConstraint(
            _in_check("status", ReservationStatus), name="ck_provider_reservations_status"
        ),
        sa.CheckConstraint(
            _in_check("failure_class", FailureClass),
            name="ck_provider_reservations_failure_class",
        ),
        sa.CheckConstraint("round >= 1", name="ck_provider_reservations_round_positive"),
        sa.UniqueConstraint("idempotency_key", name="uq_provider_reservations_idempotency_key"),
    )
    op.create_index("ix_provider_reservations_episode_id", "provider_reservations", ["episode_id"])
    op.create_index(
        "ix_provider_reservations_lookup",
        "provider_reservations",
        ["episode_id", "provider", "input_hash"],
    )

    # jobs.status の語彙は 0001 から変わっていないので触らない（変更しなかった読み手）。
    _ = JobStatus


def downgrade() -> None:
    op.drop_index("ix_provider_reservations_lookup", table_name="provider_reservations")
    op.drop_index("ix_provider_reservations_episode_id", table_name="provider_reservations")
    op.drop_table("provider_reservations")

    op.drop_index("uq_artifact_metadata_current", table_name="artifact_metadata")
    with op.batch_alter_table("artifact_metadata", schema=None) as batch:
        batch.drop_constraint("uq_artifact_metadata_version", type_="unique")
        batch.drop_column("superseded_at")
        batch.drop_column("version")
        batch.drop_column("input_hash")

    for table, checks in _PHASE1_VOCABULARY.items():
        for name, condition in checks.items():
            _recheck(table, name, condition)
