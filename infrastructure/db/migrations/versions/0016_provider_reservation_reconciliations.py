"""未照合予約の照合記録（ADR-0053）: 新しい表を1つ足すだけ（expand-only）

``provider_reservation_reconciliations``: 観測・人手確定を1行ずつ追記する。最新の1行が
その予約の判定。``provider_reservations`` には触れない。語彙は literal で凍結する。

downgrade は行が残っていれば何も変えずに拒否する（人手確定の監査記録を黙って消さない）。

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-21
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None

TABLE = "provider_reservation_reconciliations"
#: ADR-0053 の語彙。**編集しない**（履歴の凍結値）。
VERDICTS: tuple[str, ...] = (
    "confirmed_success",
    "confirmed_failed",
    "still_running",
    "cancelled",
    "unknown",
)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "reservation_id",
            sa.Uuid(),
            sa.ForeignKey("provider_reservations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("raw_status", sa.String(length=64), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("owner_approval_fingerprint", sa.String(length=16), nullable=True),
        sa.Column("actor", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            f"verdict IN ({_in(VERDICTS)})",
            name="ck_provider_reservation_reconciliations_verdict",
        ),
        sa.CheckConstraint(
            "source IN ('provider_status', 'operator')",
            name="ck_provider_reservation_reconciliations_source",
        ),
        sa.CheckConstraint(
            "source <> 'operator' OR (evidence IS NOT NULL AND owner_approval_fingerprint "
            "IS NOT NULL AND actor IS NOT NULL)",
            name="ck_provider_reservation_reconciliations_operator_fields",
        ),
    )
    op.create_index(
        "ix_provider_reservation_reconciliations_reservation_id", TABLE, ["reservation_id"]
    )


def downgrade() -> None:
    remaining = op.get_bind().execute(sa.text(f"SELECT count(*) FROM {TABLE}")).scalar_one()
    if remaining:
        raise RuntimeError(
            f"refusing to drop {TABLE}: {remaining} audit row(s) would be lost (ADR-0053)"
        )
    op.drop_index("ix_provider_reservation_reconciliations_reservation_id", table_name=TABLE)
    op.drop_table(TABLE)
