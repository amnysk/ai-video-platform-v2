"""運用異常（ADR-0027）

``operational_anomalies``: Daily Schedule の watchdog が見つけた異常（DAILY_AUTOMATION_NOT_STARTED /
SCHEDULE_PAUSED_UNEXPECTEDLY など）。``(kind, anomaly_date)`` で1日1行。語彙（CHECK）は
``contracts.schedule_guard.AnomalyKind`` から導出する。

downgrade は表を落とす（監視の記録であり、Episode 等の業務データは残る）。

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-20
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from contracts.schedule_guard import AnomalyKind

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    allowed = ", ".join(f"'{k.value}'" for k in AnomalyKind)
    op.create_table(
        "operational_anomalies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column("anomaly_date", sa.Date(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("first_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("occurrences", sa.Integer(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"kind IN ({allowed})", name="ck_operational_anomalies_kind"),
        sa.UniqueConstraint("kind", "anomaly_date", name="uq_operational_anomalies_kind_date"),
    )


def downgrade() -> None:
    op.drop_table("operational_anomalies")
